"""Explicit repository mutations and isolated HTTPS cloning; no token storage."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import quote, urlsplit

from .checkout import credentials, git_host, valid_branch
from .client import GVError


def component(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', value) or value in ('.', '..') or value.startswith('-'):
        raise GVError('Use a valid repository/owner name without paths or control characters', 'usage_error')
    return value


def source_name(value):
    if '://' in value:
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or parsed.netloc != git_host() or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise GVError('Use a canonical GitVerse HTTPS URL without credentials/query/fragment', 'invalid_repo')
        value = parsed.path.strip('/')
    parts = value.removesuffix('.git').split('/')
    if len(parts) != 2: raise GVError('Repository must be OWNER/REPO or a GitVerse HTTPS URL', 'invalid_repo')
    return '/'.join(component(p) for p in parts)


def identity(row, name):
    if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0 or not isinstance(row.get('full_name'), str) or row['full_name'].lower() != name.lower() or type(row.get('private')) is not bool:
        raise GVError('Repository identity/visibility was not confirmed', 'unexpected_response')
    return row


def user(client, deadline):
    row = client.request('GET', 'user', deadline=deadline)
    if not isinstance(row, dict): raise GVError('Unknown current API user', 'unexpected_response')
    return component(row.get('login'))


def destination(value, name):
    path = Path(os.path.abspath(value or name.split('/')[-1]))
    if os.path.lexists(path): raise GVError('Clone destination already exists; choose a new directory', 'destination_exists')
    return path


def git(args, cwd, env, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0: raise GVError('Repository operation deadline reached', 'deadline_exceeded')
    try:
        p = subprocess.run(['git', *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=remaining)
    except subprocess.TimeoutExpired as error:
        raise GVError('Git clone/verification timed out; destination was not published', 'deadline_exceeded') from error
    if p.returncode:
        raise GVError('Git clone/verification failed; destination was not published', 'git_error', details={'exit': p.returncode})
    return p.stdout.strip()


def clone(client, name, path, deadline, branch=None, depth=None, single=False, require_head=False):
    if os.path.lexists(path): raise GVError('Clone destination already exists', 'destination_exists')
    metadata = identity(client.request('GET', f'repos/{name}', deadline=deadline), name)
    settings, env = credentials(client, name, deadline=deadline)
    for key in list(env):
        if key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
                   'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE', 'GIT_SHALLOW_FILE', 'GIT_CONFIG',
                   'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT', 'GIT_TEMPLATE_DIR', 'GIT_SSL_NO_VERIFY') or key.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_')):
            env.pop(key, None)
    env['GIT_LFS_SKIP_SMUDGE'] = '1'
    settings += ['-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always']
    url = f'https://{git_host()}/{name}.git'
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.gv-clone-', dir=path.parent) as temporary:
        work = Path(temporary) / 'repository'
        options = ['clone', '--no-recurse-submodules', '--template=']
        if branch: options += ['--branch', branch]
        if depth: options += ['--depth', str(depth)]
        if single: options.append('--single-branch')
        git([*settings, *options, '--', url, str(work)], path.parent, env, deadline)
        if git([*settings, 'remote', 'get-url', 'origin'], work, env, deadline) != url:
            raise GVError('Clone origin URL was not confirmed', 'unexpected_response')
        if git([*settings, 'status', '--porcelain=v1'], work, env, deadline):
            raise GVError('Fresh clone is unexpectedly dirty', 'unexpected_response')
        # Empty repositories have an unborn HEAD; show-ref distinguishes that case.
        refs = git([*settings, 'for-each-ref', '--format=%(refname)'], work, env, deadline)
        sha = git([*settings, 'rev-parse', '--verify', 'HEAD'], work, env, deadline) if refs else None
        if require_head and sha is None: raise GVError('Fork Git contents are not ready; clone destination was not published', 'fork_not_ready')
        current = identity(client.request('GET', f'repos/{name}', deadline=deadline), name)
        if current['id'] != metadata['id']: raise GVError('Repository changed while cloning', 'unexpected_response')
        if time.monotonic() >= deadline: raise GVError('Clone deadline reached', 'deadline_exceeded')
        if os.path.lexists(path): raise GVError('Destination appeared while cloning', 'destination_exists')
        os.rename(work, path)
    return {'repository': name, 'directory': str(path), 'remote': 'origin', 'url': url, 'head_sha': sha,
            'empty': sha is None, 'submodules_initialized': False, 'lfs_smudge': False}


def partial(error, stage, name, completed):
    original = error if isinstance(error, GVError) else GVError('Local repository operation failed', 'local_error')
    details = dict(original.details or {})
    details.update(stage=stage, repository=name, completed_steps=completed, mutation_attempted=True,
                   recovery='Inspect the target repository before retrying; it may already exist. If only clone failed, use repo clone on that existing repository. No rollback or automatic POST retry.')
    return GVError(str(original), original.code, original.status, details)


def branch_head(client, name, branch, deadline):
    value = client.request('GET', f'repos/{name}/branches/{quote(branch, safe="")}', deadline=deadline)
    sha = (value.get('commit') or {}).get('sha') if isinstance(value, dict) else None
    if not isinstance(sha, str) or not re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', sha):
        raise GVError('Repository branch commit was not confirmed', 'unexpected_response')
    return sha


def repo_command(client, args):
    from .cli import repository
    deadline = time.monotonic() + args.timeout
    explicit = getattr(args, 'repo', None)
    if explicit and args.action in ('create', 'clone'):
        raise GVError('Use the positional repository/name for create/clone, not --repo', 'usage_error')
    if explicit and args.action == 'fork' and args.source and source_name(explicit).lower() != source_name(args.source).lower():
        raise GVError('Fork source and --repo disagree', 'invalid_repo')
    if args.action == 'clone':
        name = source_name(args.source)
        if args.branch: valid_branch(args.branch)
        path = destination(args.directory, name)
        if client.dry_run:
            return {'repository': name, 'directory': str(path), 'branch': args.branch, 'depth': args.depth,
                    'plan': 'Read metadata, HTTPS clone into private staging with process-only PAT helper; verify origin/HEAD then publish new directory. No submodules, hooks, stored credentials or network/writes in dry-run.'}
        return clone(client, name, path, deadline, args.branch, args.depth, args.single_branch)
    if args.dir and not args.clone: raise GVError('--dir requires --clone', 'usage_error')
    if args.action == 'create':
        parts = args.name.split('/')
        if len(parts) not in (1, 2): raise GVError('Use NAME or OWNER/NAME', 'usage_error')
        for part in parts: component(part)
        owner, name = (None, parts[0]) if len(parts) == 1 else parts
        body = {'name': name, 'private': args.private, 'description': args.description, 'auto_init': args.add_readme}
        if args.gitignore:
            if not args.add_readme: raise GVError('--gitignore requires --add-readme', 'usage_error')
            body['gitignores'] = args.gitignore
        path = destination(args.dir, name) if args.clone else None
        if client.dry_run:
            return {'owner': owner or '<current-user>', 'body': body, 'clone_directory': str(path) if path else None,
                    'plan': 'Resolve current user; POST user/repos or orgs/OWNER/repos once, verify identity/visibility with GET, optionally clone. No network/writes.'}
        me = user(client, deadline); owner = owner or me
        endpoint = 'user/repos' if owner.lower() == me.lower() else f'orgs/{owner}/repos'
        if owner.lower() != me.lower(): client.request('GET', f'orgs/{owner}', deadline=deadline)
        full = f'{owner}/{name}'
        stage, done = 'create', []
        try:
            created = identity(client.request('POST', endpoint, body=body, deadline=deadline), full)
            done.append('create'); stage = 'verify_create'
            actual = identity(client.request('GET', f'repos/{full}', deadline=deadline), full)
            if actual['id'] != created['id'] or actual['private'] != args.private:
                raise GVError('Created repository visibility/identity differs from requested', 'repository_not_confirmed')
            done.append('verify_create'); stage = 'clone'
            result = {'repository': full, 'id': actual['id'], 'private': actual['private'], 'html_url': actual.get('html_url'), 'created': True}
            if args.clone: result['clone'] = clone(client, full, path, deadline)
            return result
        except (GVError, OSError) as error: raise partial(error, stage, full, done) from error
    source = source_name(args.source) if args.source else repository(args)
    source = source_name(source)
    name = component(args.fork_name) if args.fork_name else source.split('/')[-1]
    owner = component(args.org) if args.org else None
    path = destination(args.dir, name) if args.clone else None
    body = {'name': name}
    if owner: body['organization'] = owner
    if args.description is not None: body['description'] = args.description
    if client.dry_run:
        return {'source': source, 'owner': owner or '<current-user>', 'body': body, 'clone_directory': str(path) if path else None,
                'plan': 'Read source/current user, POST fork once, poll target GET until fork parent/visibility confirmed, optionally clone. No existing remote changes, network or writes.'}
    owner = owner or user(client, deadline)
    full = f'{owner}/{name}'
    if full.lower() == source.lower(): raise GVError('Fork target equals source; use --fork-name or --org', 'usage_error')
    original = identity(client.request('GET', f'repos/{source}', deadline=deadline), source)
    source_head = None
    if args.clone and original.get('default_branch'):
        try: source_head = branch_head(client, source, original['default_branch'], deadline)
        except GVError as error:
            if error.status != 404: raise
    stage, done = 'fork', []
    try:
        accepted = client.request('POST', f'repos/{source}/forks', body=body, deadline=deadline)
        if accepted is not None: identity(accepted, full)
        done.append('fork_accepted'); stage = 'verify_fork'
        while True:
            try:
                actual = identity(client.request('GET', f'repos/{full}', deadline=deadline), full)
                break
            except GVError as error:
                if error.status != 404 or time.monotonic() >= deadline: raise
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        parent = actual.get('parent') or {}
        if not isinstance(parent, dict) or (accepted is not None and accepted['id'] != actual['id']) or actual.get('fork') is not True or parent.get('id') != original['id'] or not isinstance(parent.get('full_name'), str) or parent['full_name'].lower() != source.lower() or (original['private'] and not actual['private']):
            raise GVError('Fork parent or inherited privacy was not confirmed', 'fork_not_confirmed')
        done.append('verify_fork'); stage = 'clone'
        result = {'repository': full, 'source_repository': source, 'id': actual['id'], 'private': actual['private'],
                  'html_url': actual.get('html_url'), 'fork_accepted': True, 'parent_verified': True}
        if args.clone:
            stage = 'wait_fork_branch'
            if source_head:
                while True:
                    try:
                        branch_head(client, full, original['default_branch'], deadline)
                        break
                    except GVError as error:
                        if error.status != 404 or time.monotonic() >= deadline: raise
                        time.sleep(min(1, max(0, deadline - time.monotonic())))
            stage = 'clone'
            result['clone'] = clone(client, full, path, deadline, require_head=source_head is not None)
        return result
    except (GVError, OSError) as error: raise partial(error, stage, full, done) from error
