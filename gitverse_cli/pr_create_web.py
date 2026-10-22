"""Read-only handoff to the observed GitVerse PR creation form.

Prefill parameters are deliberately not guessed from the GitHub compare URL.
"""
import http.client
import re
import time
from urllib.parse import quote, urlencode

from .client import GVError
from .checkout import git_host
from .comment_web import open_browser
from .repositories import identity, source_name
from .pr_create import branch


def validate_web_args(args):
    if getattr(args, 'backend', 'public') != 'public':
        raise GVError('pr create --web uses public repository preflight, never web API cookies',
                      'backend_unsupported')
    conflicts = ('editor', 'draft', 'reviewer', 'no_maintainer_edit', 'attach')
    for key in conflicts:
        if getattr(args, key, None):
            raise GVError('--web is not supported with --' + key.replace('_', '-'), 'usage_error')
    # A browser form must not silently discard a requested value. Until the
    # native URL/input contract is verified, refuse before input/auth/network.
    for key in ('head', 'base'):
        value = getattr(args, key, None)
        if value is not None:
            if key == 'head' and ':' in value:
                raise GVError('Cross-repository browser prefill is not implemented', 'feature_unavailable',
                              details={'mutation_attempted': False, 'mutation_outcome': 'none'})
            branch(value)
    seeds = ('title', 'body', 'body_file', 'template', 'assignee',
             'label', 'milestone', 'project', 'recover', 'fill', 'fill_first', 'fill_verbose')
    for key in seeds:
        value = getattr(args, key, None)
        if value is not None and value is not False:
            raise GVError('GitVerse browser form prefill is not implemented for --' + key.replace('_', '-'),
                          'feature_unavailable', details={'stage': 'browser_prefill_contract',
                              'mutation_attempted': False, 'mutation_outcome': 'none',
                              'browser_launched': False})


def local_selection(repo, args, deadline):
    """Infer only from a checkout with a unique remote matching the target."""
    from .pr_create import configured_base, select_head
    from .pr_create_fill import remote_for
    from .repo_default import context
    head = args.head
    implicit = head is None and context(deadline=deadline) is not None
    if implicit:
        remote_for(repo, deadline)
        head, _ = select_head(None, repo, deadline)
    base = args.base if args.base is not None else configured_base(deadline)
    return head, base, implicit


def web_create(client, repo, args):
    validate_web_args(args)
    repo = source_name(repo)
    deadline = time.monotonic() + args.timeout
    url = f'https://{git_host()}/{repo}/compare'
    result = {'web': True, 'repository': repo, 'url': url,
              'browser_launched': False, 'pull_request_created': False,
              'mutation_attempted': False, 'mutation_outcome': 'none',
              'prefill_supported': True, 'prefill_fields': ['base', 'head'],
              'branches_selected': False, 'branches_verified': False}
    try:
        head, base, implicit = local_selection(repo, args, deadline)
        if head is not None and head == base:
            raise GVError('Head and base must be different branches', 'usage_error')
    except GVError as error:
        error.details = {**result, **(error.details or {})}
        raise
    def prefill():
        query = {key: value for key, value in (('baseBranch', base), ('headBranch', head))
                 if value is not None}
        result.update(url=url + ('?' + urlencode(query, quote_via=quote) if query else ''),
                      head=head, base=base, branches_prefilled=bool(query))
    prefill()
    if client.dry_run:
        return {**result, 'dry_run': True, 'repository_verified': False,
                'form_verified': False,
                'base_resolved': base is not None,
                'plan': 'Verify repository and selected branches, then open native /compare form. Missing base resolves from repository default online. No network/browser/write; browser UI/session and final selections remain unverified.'}
    try:
        def read():
            if time.monotonic() >= deadline:
                raise GVError('Browser preflight deadline reached', 'deadline_exceeded')
            return identity(client.request('GET', 'repos/' + repo, deadline=deadline,
                            require_complete=True, max_response_bytes=33554432), repo)
        first = read()
        result['repository_id'] = first['id']
        pin = (first['id'], first['private'], first.get('archived'))
        if first.get('archived') is not False:
            raise GVError('Browser creation requires a confirmed unarchived repository', 'repository_unavailable')
        default_base = base is None and head is not None
        if default_base:
            base = branch(first.get('default_branch'))
        if head is not None and head == base:
            raise GVError('Head and base must be different branches', 'usage_error')
        prefill()
        def branches():
            pins = {}
            for key, name in (('base', base), ('head', head)):
                if name is None:
                    continue
                if time.monotonic() >= deadline:
                    raise GVError('Browser preflight deadline reached', 'deadline_exceeded')
                row = client.request('GET', 'repos/' + repo + '/branches/' + quote(name, safe=''),
                                     deadline=deadline, require_complete=True, max_response_bytes=33554432)
                commit = row.get('commit') if isinstance(row, dict) else None
                sha = commit.get('sha') if isinstance(commit, dict) else None
                if (not isinstance(row, dict) or row.get('name') != name
                        or not isinstance(sha, str) or not re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', sha)):
                    raise GVError('Browser branch identity was not confirmed', 'unexpected_response')
                pins[key] = sha.lower()
            return pins
        branch_pins = branches()
        second = read()
        if (second['id'], second['private'], second.get('archived')) != pin:
            raise GVError('Repository changed before browser handoff', 'concurrent_change')
        if default_base and second.get('default_branch') != base:
            raise GVError('Default branch changed before browser handoff', 'concurrent_change')
        if branches() != branch_pins:
            raise GVError('Branches changed before browser handoff', 'concurrent_change')
        if implicit or args.base is None:
            if local_selection(repo, args, deadline) != (head, None if default_base else base, implicit):
                raise GVError('Local branch selection changed before browser handoff', 'concurrent_change')
        result.update(branches_verified=bool(branch_pins), branch_shas=branch_pins)
        open_browser(result['url'], deadline)
    except (OSError, http.client.HTTPException):
        raise GVError('Browser preflight transport failed; no API write', 'network_error', details=result) from None
    except GVError as error:
        error.details = {**result, **(error.details or {})}
        if error.details.get('browser_handoff') == 'unknown':
            error.details['browser_launched'] = None
        raise
    return {**result, 'repository_id': first['id'], 'repository_verified': True,
            'form_verified': False, 'browser_launched': True, 'launcher_acknowledged': True,
            'note': 'Browser handoff only. Native branch query parameters are supplied; verify the final selections and enter text in the browser. CLI has not created a PR or verified browser UI/session/permissions. Branches may move after preflight.'}
