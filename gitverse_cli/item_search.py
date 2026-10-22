"""Complete scoped multi-repository title/body search for PRs and issues."""
from .client import GVError
from .directory import user_value
from .issues import Session, issue_value
from .metadata import login_name, names
from .search import prepare, matches


def scopes(args):
    from .cli import parse_repo, repository
    if args.repos and getattr(args, 'repo', None):
        raise GVError('Use repeated --repos or one -R/context, not both', 'usage_error')
    if args.max_repos > 100: raise GVError('--max-repos must not exceed 100', 'usage_error')
    selected = args.repos or [repository(args)]
    result, seen = [], set()
    for value in selected:
        repo = parse_repo(value)
        if repo != value: raise GVError('--repos accepts canonical OWNER/REPO, not URLs', 'usage_error')
        if repo.casefold() not in seen:
            result.append(repo); seen.add(repo.casefold())
    if len(result) > args.max_repos: raise GVError('Selected repository scope bound exceeded', 'search_limit')
    return result


def selected_metadata(row, author, assignee, labels):
    checks = []
    if author:
        user = row.get('user')
        if not isinstance(user, dict) or not isinstance(user.get('login'), str) or not user['login']:
            raise GVError('Author metadata required by search filter is unknown', 'unexpected_response')
        checks.append(user['login'].casefold() == author.casefold())
    if assignee:
        if 'assignees' not in row or (row['assignees'] is not None and not isinstance(row['assignees'], list)):
            raise GVError('Assignee metadata required by search filter is unknown', 'unexpected_response')
        values = row['assignees'] or []
        if any(not isinstance(u, dict) or not isinstance(u.get('login'), str) or not u['login'] for u in values):
            raise GVError('Invalid search assignee identity', 'unexpected_response')
        checks.append(assignee.casefold() in {u['login'].casefold() for u in values})
    if labels:
        if 'labels' not in row or (row['labels'] is not None and not isinstance(row['labels'], list)):
            raise GVError('Label metadata required by search filter is unknown', 'unexpected_response')
        values = row['labels'] or []
        if any(not isinstance(u, dict) or not isinstance(u.get('name'), str) or not u['name'] for u in values):
            raise GVError('Invalid search label metadata', 'unexpected_response')
        checks.append(set(labels) <= {u['name'] for u in values})
    return all(checks)


def search_items(client, args):
    needle = prepare(args)
    for value in (args.author, args.assignee):
        if value is not None: login_name(value)
    labels = names(args.label)
    repos = scopes(args)
    base = {'kind': args.action, 'repositories': repos, 'query': args.query, 'case_sensitive': args.case_sensitive,
            'filters': {'state': args.state, 'author': args.author, 'assignee': args.assignee, 'labels': labels}}
    if client.dry_run:
        return {**base, 'dry_run': True, 'plan': 'Resolve @me; read every selected repo catalog to empty; exclude PRs for issues; apply literal title/body and AND metadata/state filters. No global index, network or partial results.'}
    session = Session(client, args)
    author, assignee = args.author, args.assignee
    if '@me' in (author, assignee):
        login = user_value(session.request('GET', 'user'))['login']
        if author == '@me': author = login
        if assignee == '@me': assignee = login
    base['filters'].update(author=author, assignee=assignee)
    results, coverage, scanned = [], [], 0
    for repo in repos:
        seen, numbers, excluded, count = set(), set(), 0, 0
        for page in range(1, args.max_pages + 1):
            path = f'repos/{repo}/' + ('pulls' if args.action == 'prs' else 'issues')
            batch = session.request('GET', path, {'state': 'all', 'page': page, 'per_page': args.page_size})
            if not isinstance(batch, list): raise GVError('Unknown issue/PR search catalog shape', 'unexpected_response')
            if not batch: break
            for row in batch:
                issue_value(row, allow_pull=True)
                if type(row.get('id')) is not int or row['id'] <= 0:
                    raise GVError('Issue/PR search identity missing', 'unexpected_response')
                if row['id'] in seen or row['number'] in numbers:
                    raise GVError('Repeated or shifting issue/PR search pages', 'pagination_error')
                seen.add(row['id']); numbers.add(row['number']); scanned += 1; count += 1
                if scanned > args.max_entries: raise GVError('Combined catalog entry bound exceeded', 'search_limit')
                if args.action == 'issues' and row.get('pull_request') is not None:
                    excluded += 1; continue
                if 'body' not in row or (row['body'] is not None and not isinstance(row['body'], str)):
                    raise GVError('Issue/PR search body is unknown', 'unexpected_response')
                if args.action == 'prs' and type(row.get('merged')) is not bool:
                    raise GVError('PR merged state is unknown', 'unexpected_response')
                metadata_match = selected_metadata(row, author, assignee, labels)
                state_match = (args.state == 'all' or (row.get('merged') is True if args.state == 'merged' else row['state'] == args.state))
                text_match = any(matches(text, needle, args) for text in (row['title'], row['body'] or ''))
                if metadata_match and state_match and text_match:
                    result = {k: row.get(k) for k in ('id', 'number', 'title', 'body', 'state', 'html_url')}
                    result['repository'] = repo
                    if args.action == 'prs': result['merged'] = row['merged']
                    results.append(result)
                    if len(results) > args.max_matches: raise GVError('Combined search match bound exceeded', 'search_limit')
        else: raise GVError('Repository page bound exceeded; no partial search returned', 'pagination_limit')
        coverage.append({'repository': repo, 'catalog_complete': True, 'entries_scanned': count, 'pull_requests_excluded': excluded})
    return {**base, 'results': results, 'total_matches': len(results),
            'coverage': {'complete': True, 'repositories': coverage, 'entries_scanned': scanned,
                         'global_search': False, 'atomic_snapshot': False,
                         'ordering': 'Selected repository order, then API catalog order'}}
