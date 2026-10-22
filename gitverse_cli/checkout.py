"""Fetch a pinned PR head; reset the target branch only with explicit force."""
from contextvars import ContextVar
import time
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from urllib.parse import urlsplit
import uuid

from .client import GVError


_deadline = ContextVar('checkout_deadline', default=None)


def git_command(*args, allowed=(0,), env=None):
    deadline = _deadline.get()
    remaining = 120 if deadline is None else min(120, deadline - time.monotonic())
    if remaining <= 0:
        raise GVError('Checkout deadline reached', 'deadline_exceeded')
    try:
        result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'submodule.recurse=false',
                                 *args], capture_output=True, text=True, timeout=remaining, env=env)
    except subprocess.TimeoutExpired as error:
        raise GVError('Git operation timed out; inspect local state before retrying', 'deadline_exceeded') from error
    if result.returncode not in allowed:
        # Git stderr can include authenticated URLs or credential-helper output.
        raise GVError('Git operation failed; inspect local state before retrying', 'git_error', details={'operation': args[0], 'exit': result.returncode})
    return result


def git_host():
    host = os.environ.get('GV_GIT_HOST', 'gitverse.ru')
    parsed = urlsplit('https://' + host)
    if not parsed.hostname or parsed.netloc != host or parsed.path or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise GVError('GV_GIT_HOST must be a hostname with an optional port', 'invalid_repo')
    return host


def pr_reference(value, explicit_repo=None):
    """Resolve only explicit numbers and canonical same-host PR URLs."""
    from .cli import parse_repo
    if value.isascii() and value.isdecimal() and int(value) > 0:
        return int(value), explicit_repo
    p = urlsplit(value)
    match = re.fullmatch(r'/([\w.-]+/[\w.-]+)/pulls/([1-9][0-9]*)/?', p.path, re.ASCII)
    if p.scheme != 'https' or p.netloc != git_host() or not match:
        raise GVError('Expected a positive PR number or https://GITVERSE/OWNER/REPO/pulls/NUMBER', 'usage_error')
    repo = parse_repo(match[1], urlsplit('https://' + git_host()).hostname)
    if explicit_repo and parse_repo(explicit_repo, urlsplit('https://' + git_host()).hostname) != repo:
        raise GVError('PR URL and --repo/GV_REPO refer to different repositories', 'invalid_repo')
    return int(match[2]), repo


def clean_worktree():
    if git_command('rev-parse', '--is-inside-work-tree').stdout.strip() != 'true':
        raise GVError('Run checkout inside a non-bare Git working tree', 'git_error')
    if git_command('status', '--porcelain=v1', '--untracked-files=normal').stdout:
        raise GVError('Working tree has changes or untracked files; commit or stash them before checkout', 'dirty_worktree')
    for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply'):
        path = Path(git_command('rev-parse', '--git-path', name).stdout.strip())
        if path.exists():
            raise GVError('Finish the current merge/rebase/cherry-pick/revert before checkout', 'git_operation_in_progress')


def valid_branch(name):
    if not isinstance(name, str) or not name or name.startswith('-') or name == 'HEAD':
        raise GVError('Invalid branch name', 'usage_error')
    result = git_command('check-ref-format', '--branch', name, allowed=(0, 128))
    if result.returncode or result.stdout.strip() != name:
        raise GVError('Invalid branch name', 'usage_error')
    return name


def valid_worktree(path):
    if path is not None and (not path or '\x00' in path):
        raise GVError('--worktree requires a nonempty path without NUL', 'usage_error')


def credentials(client, repo, deadline=None):
    """A process-only helper: no PAT in argv, URLs, Git config or new files."""
    env = dict(os.environ)
    for key in list(env):
        if key.startswith('GIT_TRACE') or key == 'GIT_CURL_VERBOSE':
            env.pop(key, None)
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GCM_INTERACTIVE'] = 'never'
    env['GIT_ASKPASS'] = ''
    env['GV_GIT_HELPER_REPO'] = repo
    env['GV_GIT_HELPER_HOST'] = git_host()
    if client.auth_source:
        user = client.request('GET', 'user', **({'deadline': deadline, 'require_complete': True, 'max_response_bytes': 1048576} if deadline is not None else {}))
        username = user.get('login') if isinstance(user, dict) else None
        if not isinstance(username, str) or not username or '\n' in username or '\r' in username:
            raise GVError('API did not return a usable Git username', 'unexpected_response')
        env['GV_GIT_HELPER_USERNAME'] = username
    helper = '!' + shlex.join([sys.executable, str(Path(__file__).with_name('git_credential.py'))])
    settings = ['-c', 'credential.helper=', '-c', 'credential.helper=' + helper,
                '-c', 'credential.useHttpPath=true', '-c', 'http.followRedirects=false',
                '-c', 'http.sslVerify=true', '-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always']
    return settings, env


def snapshot(pull, repo, number):
    from .repositories import source_name
    if not isinstance(pull, dict) or type(pull.get('number')) is not int or pull['number'] != number or type(pull.get('id')) is not int or pull['id'] <= 0:
        raise GVError('PR identity was not confirmed', 'unexpected_response')
    head, base = pull.get('head'), pull.get('base')
    if not isinstance(head, dict) or not isinstance(base, dict) or not isinstance(base.get('repo'), dict) or not isinstance(base['repo'].get('full_name'), str) or base['repo']['full_name'].lower() != repo.lower():
        raise GVError('PR base repository identity was not confirmed', 'unexpected_response')
    sha = head.get('sha')
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', sha):
        raise GVError('PR response has no valid head SHA', 'unexpected_response')
    source = head.get('repo')
    if not isinstance(source, dict) or not source.get('full_name'):
        raise GVError('PR source repository is unavailable or deleted', 'source_repo_unavailable')
    # DTO names must be names, never server-provided clone URLs.
    name = source.get('full_name')
    if not isinstance(name, str) or '://' in name or source_name(name) != name:
        raise GVError('PR source repository name is invalid', 'unexpected_response')
    return (pull['id'], sha.lower(), valid_branch(head.get('ref')), name)


def checkout_pr(args, client, repo, number):
    token = _deadline.set(time.monotonic() + args.timeout)
    try:
        return _checkout_pr(args, client, repo, number)
    finally:
        _deadline.reset(token)


def _checkout_pr(args, client, repo, number):
    from .cli import parse_repo, require_public
    require_public(client, 'pr checkout')
    valid_worktree(args.worktree)
    if args.branch is not None:
        valid_branch(args.branch)
    endpoint = f'repos/{repo}/pulls/{number}'
    if client.dry_run:
        return {'pull_request': client.request('GET', endpoint), 'local_checkout': {
            'branch': args.branch or (None if args.detach else '<head.ref>'), 'detach': args.detach,
            'force': args.force, 'worktree': args.worktree, 'timeout': args.timeout,
            'fetch': 'Pinned head.sha from the source repository; HTTPS with process-only PAT helper',
            'safety': 'Clean worktree required; --force resets only target branch. Worktree path must be new. No submodules, stash, credential storage or remote URL changes.'}}
    clean_worktree()
    destination = Path(os.path.abspath(args.worktree)) if args.worktree else None
    if destination and os.path.lexists(destination):
        raise GVError('Worktree destination already exists; choose a new path', 'destination_exists')
    local_repos = set()
    for remote in git_command('remote').stdout.splitlines():
        try:
            local_repos.add(parse_repo(git_command('remote', 'get-url', remote).stdout.strip(), urlsplit('https://' + git_host()).hostname))
        except GVError:
            continue
    if repo not in local_repos:
        raise GVError('Current clone has no GitVerse remote matching the requested repository', 'repo_mismatch')
    def read():
        return snapshot(client.request('GET', endpoint, deadline=_deadline.get(), require_complete=True,
                                       max_response_bytes=1048576), repo, number)
    pinned = read()
    _, sha, ref, source_repo = pinned
    branch = None if args.detach else valid_branch(args.branch or ref)
    before = git_command('symbolic-ref', '--quiet', '--short', 'HEAD', allowed=(0, 1)).stdout.strip() or None
    original = git_command('rev-parse', '--verify', 'HEAD', allowed=(0, 128))
    original_sha = original.stdout.strip() if original.returncode == 0 else None
    target = f'https://{git_host()}/{source_repo}.git'
    temporary_ref = 'refs/gv/checkout/' + uuid.uuid4().hex
    settings, env = credentials(client, source_repo, deadline=_deadline.get())
    local_mutation_attempted, old = False, None
    stage = 'fetch'
    try:
        git_command(*settings, 'fetch', '--no-tags', '--no-recurse-submodules', '--no-write-fetch-head', '--', target, f'{sha}:{temporary_ref}', env=env)
        fetched = git_command('rev-parse', '--verify', temporary_ref + '^{commit}').stdout.strip()
        if fetched.lower() != sha:
            raise GVError('Fetched commit does not match the PR head SHA', 'head_mismatch')
        if read() != pinned:
            raise GVError('PR changed during fetch; no checkout performed', 'pr_changed')
        clean_worktree()
        existing = None
        if branch:
            existing = git_command('show-ref', '--verify', '--hash', 'refs/heads/' + branch, allowed=(0, 128, 1))
            if existing.returncode == 0:
                old = existing.stdout.strip()
                if not args.force and git_command('merge-base', '--is-ancestor', old, fetched, allowed=(0, 1)).returncode:
                    raise GVError('Local branch has commits outside the PR head; use --branch NEW_NAME, --detach or --force', 'branch_diverged', details={'branch': branch})
        stage = 'checkout'
        local = ('-C', str(destination)) if destination else ()
        if destination:
            if os.path.lexists(destination):
                raise GVError('Worktree destination appeared during fetch', 'destination_exists')
            local_mutation_attempted = True
            if args.detach:
                git_command('worktree', 'add', '--detach', '--', str(destination), fetched)
            elif existing.returncode:
                git_command('worktree', 'add', '-b', branch, '--', str(destination), fetched)
            else:
                # Native Git refuses a branch already checked out in ANY worktree.
                git_command('worktree', 'add', '--', str(destination), branch)
        else:
            local_mutation_attempted = True
        if args.detach:
            if not destination:
                git_command('switch', '--no-overwrite-ignore', '--detach', fetched)
            action = 'detached'
        elif existing.returncode:
            if not destination:
                git_command('switch', '--no-overwrite-ignore', '-c', branch, fetched)
            action = 'created'
        elif args.force:
            git_command(*local, 'switch', '--no-overwrite-ignore', '-C', branch, fetched)
            action = 'already_current' if old == fetched else 'reset'
        else:
            if not destination:
                git_command('switch', '--no-overwrite-ignore', branch)
            if old != fetched:
                git_command(*local, 'merge', '--ff-only', '--no-edit', '--no-overwrite-ignore', fetched)
            action = 'already_current' if old == fetched else 'fast_forwarded'
        stage = 'verify'
        actual = git_command(*local, 'rev-parse', 'HEAD').stdout.strip()
        if actual != fetched:
            raise GVError('Checkout did not reach the expected commit', 'head_mismatch')
        actual_ref = git_command(*local, 'symbolic-ref', '--quiet', 'HEAD', allowed=(0, 1)).stdout.strip() or None
        expected_ref = 'refs/heads/' + branch if branch is not None else None
        if actual_ref != expected_ref:
            raise GVError('Checkout branch changed; inspect local state before retrying', 'branch_changed')
        if destination:
            original_now = git_command('rev-parse', '--verify', 'HEAD', allowed=(0, 128))
            original_ref = git_command('symbolic-ref', '--quiet', '--short', 'HEAD', allowed=(0, 1)).stdout.strip() or None
            if (original_now.stdout.strip() if original_now.returncode == 0 else None) != original_sha or original_ref != before:
                raise GVError('Original HEAD changed during worktree creation; inspect both worktrees before retrying', 'original_head_changed')
        return {'repository': repo, 'number': number, 'source_repository': source_repo,
                'head_sha': actual, 'branch': branch, 'detached': args.detach, 'action': action,
                'previous_branch': before, 'previous_sha': original_sha, 'previous_target_sha': old,
                'worktree': str(destination) if destination else None, 'submodules_updated': False}
    except GVError as error:
        details = dict(error.details or {})
        details.update(repository=repo, number=number, head_sha=sha, branch=branch,
                       previous_branch=before, previous_sha=original_sha, previous_target_sha=old,
                       worktree=str(destination) if destination else None, temporary_ref=temporary_ref,
                       stage=stage, local_mutation_attempted=local_mutation_attempted, mutation_attempted=False,
                       recovery='Inspect HEAD, target branch and git worktree list before retrying. No automatic retry or rollback; remote API is read-only.')
        raise GVError(str(error), error.code, error.status, details) from error
    finally:
        # Only our unique temporary ref. A failed cleanup must not hide recovery IDs.
        try:
            git_command('update-ref', '-d', temporary_ref, allowed=(0, 1, 128))
        except GVError:
            pass
