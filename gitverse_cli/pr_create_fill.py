"""Bounded local commit defaults for PR creation; never fetch or push."""
import os
import re
import select
import subprocess
import time
from urllib.parse import quote

from .client import GVError


MAX_BYTES = 4194304
MAX_COMMITS = 1000


def git_read(args, deadline):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_NO_LAZY_FETCH='1', GIT_NO_REPLACE_OBJECTS='1')
    # Honor neither replacement objects nor an environment-selected graft graph.
    env.pop('GIT_GRAFT_FILE', None)
    try:
        process = subprocess.Popen(['git', '-c', 'protocol.allow=never', '-c', 'core.hooksPath=/dev/null',
                                    '--no-pager', *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    except OSError as error:
        raise GVError('Local Git is unavailable', 'git_error') from error
    try:
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                raise GVError('Local text preparation deadline reached', 'deadline_exceeded')
            chunk = os.read(process.stdout.fileno(), min(65536, MAX_BYTES - size + 1))
            if not chunk:
                break
            chunks.append(chunk); size += len(chunk)
            if size > MAX_BYTES:
                raise GVError('Local Git output exceeds 4 MiB', 'input_limit')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GVError('Local text preparation deadline reached', 'deadline_exceeded')
        if process.wait(timeout=remaining):
            raise GVError('Local Git read failed; fill requires existing local base/head branches', 'git_error')
        try:
            return b''.join(chunks).decode('utf-8')
        except UnicodeError as error:
            raise GVError('Commit text must be UTF-8', 'usage_error') from error
    except subprocess.TimeoutExpired as error:
        raise GVError('Local text preparation deadline reached', 'deadline_exceeded') from error
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def remote_for(repo, deadline):
    from .cli import parse_repo
    matches = []
    for remote in git_read(['remote'], deadline).splitlines():
        if not remote or remote.startswith('-') or any(ord(c) < 33 for c in remote):
            raise GVError('Invalid local remote name', 'git_error')
        url = git_read(['remote', 'get-url', '--', remote], deadline).strip()
        try:
            candidate = parse_repo(url, os.getenv('GV_GIT_HOST', 'gitverse.ru'))
        except (GVError, ValueError):
            continue
        if candidate.casefold() == repo.casefold():
            matches.append(remote)
    if len(matches) != 1:
        raise GVError('Fill requires exactly one GitVerse remote matching the selected repository', 'ambiguous_remote')
    return matches[0]


def pin(ref, deadline):
    value = git_read(['rev-parse', '--verify', ref + '^{commit}'], deadline).strip()
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value):
        raise GVError('Invalid local commit identity', 'git_error')
    return value


def prepare(repo, base, head, deadline, *, first=False, verbose=False):
    if git_read(['rev-parse', '--is-shallow-repository'], deadline).strip() != 'false':
        raise GVError('Fill requires complete local history; shallow history refused', 'incomplete_history')
    # Grafts alter the graph independently of replacement refs (disabled above).
    from pathlib import Path
    grafts = git_read(['rev-parse', '--git-path', 'info/grafts'], deadline).strip()
    if Path(grafts).exists():
        raise GVError('Fill refuses grafted history', 'incomplete_history')
    remote = remote_for(repo, deadline)
    refs = {'base': f'refs/remotes/{remote}/{base}', 'head': f'refs/heads/{head}'}
    pins = {key: pin(ref, deadline) for key, ref in refs.items()}
    # Match gh's right-side, non-merge comparison and native Git log ordering.
    data = git_read(['-c', 'log.ShowSignature=false', 'log', '--encoding=UTF-8',
                     '--pretty=format:%H%x00%s%x00%b%x00', '--cherry',
                     '--max-count=' + str(MAX_COMMITS + 1),
                     pins['base'] + '...' + pins['head'], '--'], deadline)
    parts = data.split('\0')
    if len(parts) == 1 and not data:
        raise GVError('No commits between selected base and head', 'empty_commit_range')
    if len(parts) % 3 != 1 or parts[-1]:
        raise GVError('Invalid commit message framing', 'unexpected_response')
    commits = []
    for i in range(0, len(parts) - 1, 3):
        identifier = parts[i].removeprefix('\n')
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', identifier):
            raise GVError('Invalid commit message identity', 'unexpected_response')
        commits.append((parts[i + 1], parts[i + 2]))
    if len(commits) > MAX_COMMITS:
        raise GVError('Fill commit limit reached; no partial defaults', 'input_limit')
    if len(commits) == 1 or first:
        # gh selects the last entry in native newest-first --cherry log order,
        # not HEAD and not a timestamp-sorted or first-parent approximation.
        title, body = commits[-1]
    else:
        title = head.replace('_', ' ').replace('-', ' ')
        sections = []
        for index, (subject, description) in enumerate(reversed(commits)):
            sections.append('- **' + subject + '**\n')
            if verbose:
                # gh indents every line start, including an empty body or the
                # empty final line after LF; two extra LFs separate commits.
                sections.append(re.sub(r'(?m)^', '  ', description))
                if index < len(commits) - 1:
                    sections.append('\n\n')
        body = ''.join(sections)
    result = {'remote': remote, 'refs': refs, 'pins': pins, 'commits': len(commits)}
    if first:
        result['mode'] = 'first'
    elif verbose:
        result['mode'] = 'verbose'
    recheck(result, repo, deadline)
    return title, body, result


def recheck(context, repo, deadline):
    if (remote_for(repo, deadline) != context['remote']
            or {key: pin(ref, deadline) for key, ref in context['refs'].items()} != context['pins']):
        raise GVError('Local fill refs changed; no PR submitted', 'concurrent_change')


def verify_remote(client, repo, base, head, context, source=None):
    if source:
        if [source['base_sha'], source['head_sha']] != [context['pins']['base'], context['pins']['head']]:
            raise GVError('Qualified source differs from local fill pins', 'concurrent_change')
        return
    for key, name in (('base', base), ('head', head)):
        row = client.request('GET', f'repos/{repo}/branches/{quote(name, safe="")}')
        if (not isinstance(row, dict) or row.get('name') != name
                or not isinstance(row.get('commit'), dict)
                or row['commit'].get('sha') != context['pins'][key]):
            raise GVError('Remote branch differs from pinned local fill history', 'concurrent_change')
