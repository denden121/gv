"""Per-checkout default repository in local Git config, separate from PAT/settings."""
import os
import subprocess
import time

from .client import Client, GVError
from .repositories import identity

KEY = 'gitverse.defaultRepository'


def command(*args, allowed=(0,), deadline=None):
    remaining = 60 if deadline is None else min(60, deadline - time.monotonic())
    if remaining <= 0: raise GVError('Local default-repository deadline reached', 'deadline_exceeded')
    try: result = subprocess.run(['git', *args], capture_output=True, text=True, timeout=remaining)
    except subprocess.TimeoutExpired as error: raise GVError('Git default-repository operation timed out', 'deadline_exceeded') from error
    if result.returncode not in allowed:
        raise GVError('Git default-repository operation failed', 'git_error', details={'exit': result.returncode})
    return result


def context(required=False, deadline=None):
    result = command('rev-parse', '--absolute-git-dir', allowed=(0, 128), deadline=deadline)
    if result.returncode:
        if required: raise GVError('Run repo set-default inside a Git repository', 'git_required')
        return None
    return result.stdout.strip()


def values(deadline=None):
    result = command('config', '--local', '--no-includes', '--null', '--get-all', KEY, allowed=(0, 1), deadline=deadline)
    if result.returncode == 1: return []
    if not result.stdout.endswith('\0'): raise GVError('Unknown local Git config value format', 'invalid_local_default')
    return result.stdout[:-1].split('\0')


def canonical(value):
    from .cli import parse_repo
    try: parsed = parse_repo(value)
    except (GVError, ValueError): raise GVError('Invalid local default repository; use repo set-default --unset to repair', 'invalid_local_default') from None
    if value != parsed:
        raise GVError('Local default must be canonical OWNER/REPO; use --unset to repair', 'invalid_local_default')
    return parsed


def read_default():
    if context() is None: return None
    stored = values()
    if not stored: return None
    if len(stored) != 1: raise GVError('Multiple local default repositories; use repo set-default --unset to repair', 'invalid_local_default')
    return canonical(stored[0])


def selected_remote(selected, remotes):
    matches = {repo for repo in remotes.values() if repo.casefold() == selected.casefold()}
    if not matches:
        raise GVError('Saved local default no longer matches a GitVerse remote; use --unset or choose a current remote', 'stale_local_default')
    return selected


def inventory(deadline):
    from .cli import parse_repo
    remotes = {}
    for remote in command('remote', deadline=deadline).stdout.splitlines():
        if not remote or remote.startswith('-') or any(ord(c) < 32 or ord(c) == 127 for c in remote):
            raise GVError('Invalid Git remote name', 'git_error')
        url = command('remote', 'get-url', '--', remote, deadline=deadline).stdout.strip()
        try: remotes[remote] = parse_repo(url, os.getenv('GV_GIT_HOST', 'gitverse.ru'))
        except (GVError, ValueError): continue
    return remotes


def set_default(args):
    if getattr(args, 'repo', None) or hasattr(args, 'backend'):
        raise GVError('repo set-default uses the current Git context and positional OWNER/REPO, not -R/--backend', 'usage_error')
    target = args.default_repository
    if (args.view or args.unset) and target is not None:
        raise GVError('Do not combine a repository argument with --view/--unset', 'usage_error')
    if not (args.view or args.unset) and target is None:
        raise GVError('Pass OWNER/REPO, --view or --unset; no interactive selection', 'usage_error')
    if target is not None: target = canonical(target)
    deadline = time.monotonic() + args.timeout
    git_dir = context(True, deadline)
    before = values(deadline)
    remotes = inventory(deadline)
    base = {'scope': 'local_git_config', 'git_dir': git_dir, 'key': KEY,
            'remotes': [{'name': name, 'repository': repo} for name, repo in remotes.items()]}
    if args.view:
        if len(before) > 1: raise GVError('Multiple local defaults; use --unset to repair', 'invalid_local_default')
        selected = selected_remote(canonical(before[0]), remotes) if before else None
        choices = {repo.casefold(): repo for repo in remotes.values()}
        return {**base, 'default_repository': selected, 'inferred_repository': next(iter(choices.values())) if not selected and len(choices) == 1 else None}
    if args.unset: target_values = []
    else:
        selected_remote(target, remotes)
        target_values = [target]
    if getattr(args, 'dry_run', False):
        return {**base, 'dry_run': True, 'default_repository': target if not args.unset else None,
                'plan': 'Validate local remote selection; set requires public API identity check; persist only local Git config and verify. No network or config writes.'}
    if before == target_values:
        return {**base, 'changed': False, 'default_repository': target if not args.unset else None}
    if not args.unset:
        client = Client()
        identity(client.request('GET', 'repos/' + target, deadline=deadline, require_complete=True, max_response_bytes=33554432), target)
    if context(True, deadline) != git_dir or values(deadline) != before or inventory(deadline) != remotes:
        raise GVError('Git context/default/remotes changed before saving; no config write attempted', 'concurrent_change')
    stage = 'write'
    try:
        if args.unset: command('config', '--local', '--no-includes', '--unset-all', KEY, deadline=deadline)
        else: command('config', '--local', '--no-includes', '--replace-all', KEY, target, deadline=deadline)
        stage = 'verify'
        if context(True, deadline) != git_dir or values(deadline) != target_values:
            raise GVError('Local Git default did not match requested value after write', 'verification_failed')
        return {**base, 'changed': True, 'default_repository': target if not args.unset else None,
                'verification': {'readback': True, 'atomic_compare_and_swap': False}}
    except GVError as error:
        error.details = {'stage': stage, 'local_write_attempted': True,
                         'recovery': 'Read repo set-default --view or explicitly --unset before retrying; no automatic rollback'}
        raise
