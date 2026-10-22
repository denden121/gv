"""PR selection and status. Branch matches must be complete and unambiguous."""
import re
import subprocess
from types import SimpleNamespace

from .client import GVError


def scan_pulls(client, repo, query, max_pages=100):
    rows, seen = [], set()
    for page in range(1, max_pages + 1):
        result = client.request('GET', f'repos/{repo}/pulls', {**query, 'page': page, 'per_page': 100})
        total = None
        if isinstance(result, dict) and isinstance(result.get('items'), list):
            total = result.get('total_count')
            result = result['items']
        if not isinstance(result, list) or (total is not None and (type(total) is not int or total < 0)):
            raise GVError('Unknown PR pagination response', 'unexpected_response')
        for pull in result:
            if not isinstance(pull, dict) or type(pull.get('number')) is not int or pull['number'] <= 0 or pull.get('state') not in ('open', 'closed'):
                raise GVError('Invalid PR identity/state in list response', 'unexpected_response')
            if pull['number'] in seen:
                raise GVError('PR pages repeat or shifted during collection; selection is incomplete', 'pagination_error')
            seen.add(pull['number'])
        rows.extend(result)
        if total is not None and total < len(rows):
            raise GVError('PR total_count contradicts returned pages', 'pagination_error')
        if total is not None and len(rows) >= total:
            return rows
        if not result:
            if total is not None and len(rows) < total:
                raise GVError('Empty PR page before total_count', 'pagination_error')
            return rows
        # Continue to an empty page even after a short page: servers may cap
        # per_page below 100. Never choose a unique PR from a truncated list.
    raise GVError('PR pagination bound reached; use an explicit PR number/URL', 'pagination_limit', details={'max_pages': max_pages})


def list_pulls(args, client, repo):
    from .cli import require_public
    from .checkout import valid_branch
    from .metadata import names, login_name, login_values, objects
    require_public(client, 'pr list')
    state = 'closed' if args.state == 'merged' else args.state
    path = f'repos/{repo}/pulls'
    filtered = bool(args.head or args.base or args.author or args.assignee or args.label or args.draft or args.no_draft or args.reviewer or args.search_text or args.state == 'merged')
    if not filtered:
        return client.pages(path, {'state': state, 'page': args.page}, args.paginate, min(args.limit, 100), max_pages=args.max_pages)
    spec = branch_spec(args.head) if args.head else None
    if args.base:
        valid_branch(args.base)
    wanted_labels = names(args.label)
    people = {k: login_name(getattr(args, k)) for k in ('author', 'assignee', 'reviewer') if getattr(args, k)}
    if client.dry_run:
        return {'read_request': client.request('GET', path, {'state': state, 'page': 1, 'per_page': 100}),
                'head': spec, 'base': args.base, 'filters': {**people, 'labels_all': wanted_labels, 'draft': True if args.draft else False if args.no_draft else None, 'merged': args.state == 'merged', 'search_text': args.search_text},
                'plan': 'Resolve @me without mutation, scan every PR page, filter exact metadata locally, then apply --page/--limit. Search text is literal title/body substring, not GitHub query syntax.'}
    people = {k: login_values([v], client)[0].lower() for k, v in people.items()}
    rows = scan_pulls(client, repo, {'state': state}, args.max_pages)
    rows = [p for p in rows if state == 'all' or p['state'] == state]
    if spec:
        rows = matching(rows, spec, open_only=False)
    selected = []
    for pull in rows:
        if args.base:
            if not isinstance(pull.get('base'), dict) or not isinstance(pull['base'].get('ref'), str):
                raise GVError('PR list has no base.ref', 'unexpected_response')
            if pull['base']['ref'] != args.base:
                continue
        if args.state == 'merged':
            if type(pull.get('merged')) is not bool:
                raise GVError('PR merged state is unknown', 'unexpected_response')
            if not pull['merged']:
                continue
        if args.draft or args.no_draft:
            if type(pull.get('draft')) is not bool:
                raise GVError('PR draft state is unknown', 'unexpected_response')
            if pull['draft'] != bool(args.draft):
                continue
        if 'author' in people:
            author = pull.get('user')
            if not isinstance(author, dict) or not isinstance(author.get('login'), str):
                raise GVError('PR author is unknown', 'unexpected_response')
            if author['login'].lower() != people['author']:
                continue
        if 'assignee' in people and people['assignee'] not in {r['login'].lower() for r in objects(pull, 'assignees', 'login')}:
            continue
        if 'reviewer' in people and people['reviewer'] not in {r['login'].lower() for r in objects(pull, 'requested_reviewers', 'login')}:
            continue
        if wanted_labels and not set(wanted_labels) <= {r['name'] for r in objects(pull, 'labels', 'name')}:
            continue
        if args.search_text:
            if not isinstance(pull.get('title'), str) or (pull.get('body') is not None and not isinstance(pull['body'], str)):
                raise GVError('PR title/body is unknown', 'unexpected_response')
            if args.search_text.casefold() not in (pull['title'] + '\n' + (pull.get('body') or '')).casefold():
                continue
        selected.append(pull)
    selected.sort(key=lambda p: p['number'], reverse=True)
    size = min(args.limit, 100); start = (args.page - 1) * size
    return selected[start:] if args.paginate else selected[start:start + size]


def local_head():
    from .checkout import git_host
    from .cli import git, parse_repo
    branch = git('branch', '--show-current')
    if not branch:
        raise GVError('Detached HEAD: pass a PR number, URL or explicit branch', 'no_current_branch')
    remotes = {}
    for remote in git('remote').splitlines():
        try:
            remotes[remote] = parse_repo(git('remote', 'get-url', remote), git_host())
        except GVError:
            continue
    if not remotes:
        raise GVError('No GitVerse source remote for the current branch; pass an explicit selector', 'repo_required')
    # Read optional Git configuration without exposing its output in errors.
    def config(key):
        result = subprocess.run(['git', 'config', '--get', key], capture_output=True, text=True, timeout=30)
        if result.returncode not in (0, 1):
            raise GVError('Cannot read branch upstream configuration', 'git_error')
        return result.stdout.strip() if result.returncode == 0 else None
    remote = config(f'branch.{branch}.remote')
    merge = config(f'branch.{branch}.merge')
    if remote is not None:
        if remote not in remotes or not merge or not merge.startswith('refs/heads/'):
            raise GVError('Current branch upstream is not an identifiable GitVerse branch; pass an explicit selector', 'ambiguous_head_repo')
        return {'local_branch': branch, 'branch': merge[len('refs/heads/'):], 'repository': remotes[remote], 'source': 'upstream'}
    repos = set(remotes.values())
    if len(repos) != 1:
        raise GVError('Multiple possible source repositories for current branch; pass an explicit selector', 'ambiguous_head_repo')
    return {'local_branch': branch, 'branch': branch, 'repository': next(iter(repos)), 'source': 'local_branch'}


def branch_spec(value):
    from .checkout import valid_branch
    parts = value.split(':')
    if len(parts) > 3 or not parts[-1] or any(not re.fullmatch(r'[\w.-]+', p, re.ASCII) or p in ('.', '..') for p in parts[:-1]):
        raise GVError('Use BRANCH, OWNER:BRANCH or OWNER:REPO:BRANCH', 'usage_error')
    valid_branch(parts[-1])
    return {'branch': parts[-1], 'owner': parts[0] if len(parts) > 1 else None,
            'repository': '/'.join(parts[:2]) if len(parts) == 3 else None}


def matching(pulls, spec, open_only=True):
    matches = []
    for pull in pulls:
        if open_only and pull['state'] != 'open':
            continue
        head = pull.get('head')
        if not isinstance(head, dict) or not isinstance(head.get('ref'), str):
            raise GVError('PR list has no head.ref; use a number/URL', 'unexpected_response')
        if head['ref'] != spec['branch']:
            continue
        if spec.get('repository') or spec.get('owner'):
            source = head.get('repo')
            full_name = source.get('full_name') if isinstance(source, dict) else None
            if not isinstance(full_name, str) or full_name.count('/') != 1:
                raise GVError('Matching PR has no source repository identity; use a number/URL', 'unexpected_response')
            if spec.get('repository') and full_name.lower() != spec['repository'].lower():
                continue
            if spec.get('owner') and full_name.split('/')[0].lower() != spec['owner'].lower():
                continue
        matches.append(pull)
    return sorted(matches, key=lambda p: p['number'])


def resolve_branch(client, repo, selector):
    spec = branch_spec(selector) if selector is not None else local_head()
    # The documented head filter returned HTTP 400 for a slash branch in live
    # testing. Scan all open PRs and compare exact identities locally instead.
    query = {'state': 'open'}
    if client.dry_run:
        return {'selector': spec, 'request': client.request('GET', f'repos/{repo}/pulls', {**query, 'page': 1, 'per_page': 100}),
                'plan': 'Read all open PR pages without the unreliable head API filter, check exact head.ref/source repository, require exactly one open PR. No mutation without a resolved number.'}
    pulls = matching(scan_pulls(client, repo, query), spec)
    if not pulls:
        raise GVError('No open PR matches this head branch; use a number/URL for a closed PR', 'pr_not_found')
    if len(pulls) != 1:
        raise GVError('Several open PRs match this branch; use an explicit number/URL', 'ambiguous_pr', details={'numbers': [p['number'] for p in pulls]})
    return pulls[0]['number']


def summary(pull):
    keys = ('number', 'title', 'state', 'draft', 'html_url', 'user', 'head', 'base', 'requested_reviewers', 'requested_teams', 'mergeable')
    # Avoid embedding repository settings/user profiles in each status row.
    result = {k: pull.get(k) for k in keys if k not in ('user', 'head', 'base')}
    result['author'] = (pull.get('user') or {}).get('login')
    for side in ('head', 'base'):
        branch = pull.get(side) or {}
        if not isinstance(branch, dict) or (branch.get('repo') is not None and not isinstance(branch['repo'], dict)):
            raise GVError('Invalid PR branch/repository metadata', 'unexpected_response')
        result[side] = {'ref': branch.get('ref'), 'sha': branch.get('sha'), 'repository': (branch.get('repo') or {}).get('full_name')}
    result['review_decision'] = None
    return result


def pr_status(args, client, repo):
    from .cli import require_public
    require_public(client, 'pr status')
    try:
        context = local_head()
        context_error = None
    except GVError as error:
        context, context_error = None, error.code
    if client.dry_run:
        return {'repository': repo, 'current_branch': context, 'current_branch_reason': context_error,
                'user_request': client.request('GET', 'user'),
                'pulls_request': client.request('GET', f'repos/{repo}/pulls', {'state': 'open', 'page': 1, 'per_page': 100}),
                'plan': 'Paginate all open PRs; group exact current source branch, author, and direct requested reviewers. Team membership/review requirements unknown.', 'load_checks': args.checks}
    user = client.request('GET', 'user')
    login = user.get('login') if isinstance(user, dict) else None
    if not isinstance(login, str) or not login:
        raise GVError('Current user has no login', 'unexpected_response')
    pulls = [p for p in scan_pulls(client, repo, {'state': 'open'}, args.max_pages) if p['state'] == 'open']
    mine, requested = [], []
    for pull in pulls:
        author = pull['user'].get('login') if isinstance(pull.get('user'), dict) else None
        reviewers = pull.get('requested_reviewers')
        if not isinstance(author, str) or not isinstance(reviewers, list) or any(not isinstance(r, dict) or not isinstance(r.get('login'), str) for r in reviewers):
            raise GVError('PR author/review-request fields incomplete', 'unexpected_response')
        if author.lower() == login.lower():
            mine.append(pull)
        if any(r['login'].lower() == login.lower() for r in reviewers):
            requested.append(pull)
    current = matching(pulls, context) if context else []
    relevant = {p['number']: p for p in current + mine + requested}
    checks = {}
    if args.checks:
        if len(relevant) > args.checks_limit:
            raise GVError('Too many relevant PRs for CI collection; raise --checks-limit or inspect pr checks individually', 'checks_limit', details={'count': len(relevant), 'limit': args.checks_limit})
        from .checks import pr_checks
        options = SimpleNamespace(required=False, ci_only=True, watch=False, fail_fast=False, timeout=args.checks_timeout, max_pages=args.max_pages)
        for number in sorted(relevant):
            result = pr_checks(options, client, repo, number)
            checks[number] = {k: v for k, v in result.payload.items() if k not in ('checks', 'note', 'repository', 'number')}
            listed_head = relevant[number].get('head')
            listed_sha = listed_head.get('sha') if isinstance(listed_head, dict) else None
            checks[number]['listed_head_changed'] = listed_sha != result.payload.get('head_sha')
            if checks[number]['listed_head_changed']:
                checks[number]['coverage']['scope_complete'] = False
                checks[number]['exit_code'] = 3
                checks[number]['reason'] = 'status_snapshot_changed'
    def render(values):
        return [{**summary(p), 'ci': checks.get(p['number'])} for p in sorted(values, key=lambda p: p['number'], reverse=True)]
    return {'repository': repo, 'login': login,
            'current_branch': {'context': context, 'reason': context_error, 'ambiguous': len(current) > 1, 'pull_requests': render(current)},
            'created_by_you': render(mine), 'review_requested': render(requested), 'open_prs_scanned': len(pulls),
            'coverage': {'open_pr_scan_complete': True, 'direct_review_requests_known': True, 'team_membership_known': False,
                         'review_requirements_known': False, 'ci_loaded': bool(args.checks)},
            'note': 'Open PRs in this repository only. Team membership and approval requirements are unknown. CI is a separate optional snapshot; status exit 0 is not CI success or permission to merge.'}
