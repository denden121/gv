"""Read-only browser handoff for PR discussion creation or own-last editing."""
import http.client
import os
import shlex
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit

from .client import GVError
from .comment_edit import comment_snapshot
from .directory import user_value
from .pr_close import snapshot
from .pr_comment import newest_own


def open_browser(url, deadline):
    """Acknowledge launcher success, never claim browser form submission."""
    default = '/usr/bin/open' if sys.platform == 'darwin' else 'xdg-open'
    try:
        command = os.environ.get('GV_BROWSER') or default
        if len(command.encode('utf-8')) > 8192: raise ValueError()
        argv = shlex.split(command)
        if not argv or len(argv) > 64 or any('\0' in a for a in argv): raise ValueError()
    except (ValueError, UnicodeError):
        raise GVError('Invalid GV_BROWSER command', 'usage_error') from None
    remaining = deadline - time.monotonic()
    if remaining <= 0: raise GVError('Browser handoff deadline reached', 'deadline_exceeded')
    env = {k: v for k, v in os.environ.items() if k not in
           ('GITVERSE_TOKEN', 'GV_TOKEN', 'GITVERSE_COOKIE_FILE', 'GITVERSE_CSRF_TOKEN')}
    try:
        process = subprocess.Popen([*argv, url], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env=env, start_new_session=True)
    except (OSError, ValueError):
        raise GVError('Could not start browser launcher; no API write', 'browser_error') from None
    try:
        status = process.wait(timeout=max(0, deadline - time.monotonic()))
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        process.wait()
        raise GVError('Browser launcher interrupted or timed out; handoff outcome unknown, no API write',
                      'browser_error', details={'browser_handoff': 'unknown'}) from None
    if status:
        raise GVError('Browser launcher failed; handoff outcome unknown, no API write',
                      'browser_error', details={'browser_handoff': 'unknown'})


def web_comment(client, args):
    from .cli import repository, require_public
    from .checkout import git_host, pr_reference
    from .pulls import resolve_branch
    require_public(client, 'pr comment --web')
    deadline = time.monotonic() + args.timeout

    class Reads:
        dry_run = client.dry_run
        entries = 0

        def request(self, method, path, query=None, **kwargs):
            if method != 'GET': raise GVError('Browser handoff is read-only', 'usage_error')
            if time.monotonic() >= deadline:
                raise GVError('Browser handoff deadline reached', 'deadline_exceeded')
            try:
                result = client.request('GET', path, query, deadline=deadline,
                                        require_complete=True, max_response_bytes=33554432)
            except (OSError, http.client.HTTPException):
                raise GVError('Browser preflight transport failed; no API write', 'network_error') from None
            if path == f'repos/{repo}/pulls':
                rows = result.get('items') if isinstance(result, dict) else result
                if isinstance(rows, list):
                    self.entries += len(rows)
                    if self.entries > args.max_entries:
                        raise GVError('PR selection entry bound reached; browser not opened', 'catalog_limit')
            return result

    reads = Reads()
    selector = args.selector
    if selector is not None and (selector.isdecimal() or '://' in selector or selector.startswith('-')):
        if '://' in selector:
            try:
                parsed = urlsplit(selector)
                if parsed.query or parsed.fragment or any(ord(c) < 33 or ord(c) == 127 for c in selector):
                    raise ValueError()
            except ValueError:
                raise GVError('Use a canonical PR URL without query or fragment', 'usage_error') from None
        number, url_repo = pr_reference(selector, getattr(args, 'repo', None) or os.environ.get('GV_REPO'))
        if url_repo: args.repo = url_repo
        repo = repository(args)
    else:
        repo = repository(args)
        number = resolve_branch(reads, repo, selector)
        if client.dry_run:
            return {'dry_run': True, 'web': True, 'repository': repo, 'selection': number,
                    'browser_launched': False, 'mutation_attempted': False,
                    'plan': 'Resolve unique open PR, then open discussion or newest own comment. No browser or network.'}
    url = f'https://{git_host()}/{repo}/pulls/{number}'
    identity = {'repository': repo, 'number': number, 'comment_id': None}
    edit = args.edit_last
    if client.dry_run:
        return {**identity, 'dry_run': True, 'web': True, 'url': None if edit else url,
                'url_verified': False, 'edit_last': edit, 'create_if_none': args.create_if_none,
                'browser_launched': False, 'mutation_attempted': False,
                'plan': 'No network/browser/write. Verify PR; for edit select newest own discussion by creation time then ID. Absence refuses unless create-if-none; then open new discussion. Never predicts existence/selected ID or submits text.'}
    parent = f'repos/{repo}/pulls/{number}'

    def parent_snapshot():
        row = reads.request('GET', parent)
        pinned = snapshot(row, repo, number)
        # Do not hand an API-supplied arbitrary origin/query/credential URL to
        # the browser. The native PR route must match the selected identity.
        if not isinstance(row.get('html_url'), str) or row['html_url'].casefold() != url.casefold():
            raise GVError('PR browser URL does not match the selected canonical route', 'unexpected_response')
        return pinned

    before = parent_snapshot()
    selected = None
    if edit:
        user = user_value(reads.request('GET', 'user'))
        comments = f'repos/{repo}/issues/{number}/comments'
        expected = client.base.rstrip('/') + f'/repos/{repo}/issues/{number}'
        selected = newest_own(reads.request, comments, expected, user['id'], args.max_entries, deadline)
        if selected is None and not args.create_if_none:
            raise GVError('No own discussion comment; use --create-if-none to open a new comment', 'comment_not_found')
        if selected is not None:
            actual = comment_snapshot(reads.request('GET', f'repos/{repo}/issues/comments/{selected["id"]}'),
                                      selected['id'], expected, user['id'])
            if actual != selected:
                raise GVError('Selected comment changed before browser handoff', 'concurrent_change')
        latest = newest_own(reads.request, comments, expected, user['id'], args.max_entries, deadline)
        if latest != selected:
            raise GVError('Own-last comment selection changed before browser handoff', 'concurrent_change')
    if parent_snapshot() != before:
        raise GVError('PR changed before browser handoff', 'concurrent_change')
    if selected is not None:
        identity['comment_id'] = selected['id']
        url += '#comment-' + str(selected['id'])
    try:
        open_browser(url, deadline)
    except GVError as error:
        error.details = {**(error.details or {}), **identity, 'url': url, 'mutation_attempted': False}
        raise
    return {**identity, 'web': True, 'url': url, 'browser_launched': True,
            'launcher_acknowledged': True, 'comment_submitted': False, 'mutation_attempted': False,
            'mode': 'edit' if selected else 'create',
            'note': 'Browser handoff only; sign in separately and write/edit in the discussion UI. No form focus, automatic edit activation or submission guarantee.'}
