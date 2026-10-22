"""Literal message search in a pinned, closed, reachable commit ancestry graph."""
import time
from urllib.parse import quote

from .client import GVError
from .contents import get, resolve_ref, sha
from .search import matches, prepare


def commit(row):
    if not isinstance(row, dict): raise GVError('Invalid commit object', 'unexpected_response')
    identifier = sha(row.get('sha'))
    metadata = row.get('commit')
    if not isinstance(metadata, dict) or not isinstance(metadata.get('message'), str):
        raise GVError('Commit message is missing', 'unexpected_response')
    parents = row.get('parents')
    if not isinstance(parents, list): raise GVError('Commit parent list is missing', 'unexpected_response')
    identifiers = []
    for parent in parents:
        if not isinstance(parent, dict): raise GVError('Invalid commit parent', 'unexpected_response')
        identifiers.append(sha(parent.get('sha')))
    if identifier in identifiers or len(set(identifiers)) != len(identifiers):
        raise GVError('Self-parent or duplicate commit parents', 'unexpected_response')
    return {'sha': identifier, 'message': metadata['message'], 'parents': identifiers}


def graph_complete(rows, head):
    if any(parent not in rows for row in rows.values() for parent in row['parents']): return False
    # Closed graphs must also be acyclic; iterative Kahn traversal avoids
    # recursion limits on real repository histories.
    children = {identifier: [] for identifier in rows}
    pending = {identifier: len(row['parents']) for identifier, row in rows.items()}
    for identifier, row in rows.items():
        for parent in row['parents']: children[parent].append(identifier)
    roots = [identifier for identifier, count in pending.items() if count == 0]
    visited = 0
    while roots:
        identifier = roots.pop(); visited += 1
        for child in children[identifier]:
            pending[child] -= 1
            if pending[child] == 0: roots.append(child)
    if visited != len(rows): raise GVError('Commit parent graph contains a cycle', 'unexpected_response')
    reachable, todo = set(), [head]
    while todo:
        identifier = todo.pop()
        if identifier in reachable: continue
        reachable.add(identifier); todo.extend(rows[identifier]['parents'])
    if reachable != set(rows):
        raise GVError('Commit list includes history outside pinned ancestry', 'unexpected_history')
    return True


def search_commits(client, repo, args):
    needle = prepare(args)
    if args.page_size > 50: raise GVError('Commit API --page-size maximum is 50', 'usage_error')
    base = {'repository': repo, 'query': args.query, 'case_sensitive': args.case_sensitive}
    if client.dry_run:
        return {**base, 'ref': args.ref or '<default-branch>', 'plan': 'Resolve and verify pinned SHA; scan bounded commit pages; require complete acyclic reachable parent graph; literal message search. No network or partial matches.'}
    deadline = time.monotonic() + args.timeout
    ref, head = resolve_ref(client, repo, args.ref, deadline, args)
    pinned = commit(get(client, f'repos/{repo}/commits/{quote(head, safe="")}', None, deadline))
    if pinned['sha'] != head: raise GVError('Pinned commit identity mismatch', 'unexpected_response')
    rows, order = {}, []
    for page in range(1, args.max_pages + 1):
        values = get(client, f'repos/{repo}/commits', {'sha': head, 'page': page, 'per_page': args.page_size}, deadline)
        if not isinstance(values, list): raise GVError('Unknown commit history envelope', 'unexpected_response')
        if not values: raise GVError('Commit pages ended before pinned ancestry was complete', 'incomplete_history')
        for value in values:
            row = commit(value)
            if row['sha'] in rows: raise GVError('Repeated or shifting commit pages', 'pagination_error')
            rows[row['sha']] = row; order.append(row['sha'])
            if len(rows) > args.max_entries: raise GVError('Commit history entry bound reached', 'search_limit')
        if page == 1 and (order[0] != head or rows[head] != pinned):
            raise GVError('Commit list does not start with verified pinned commit', 'unexpected_history')
        if graph_complete(rows, head):
            results = [rows[identifier] for identifier in order if matches(rows[identifier]['message'], needle, args)]
            if len(results) > args.max_matches: raise GVError('Matched commit bound reached; no partial result returned', 'search_limit')
            return {**base, 'ref': ref, 'commit_sha': head, 'results': results, 'total_matches': len(results),
                    'coverage': {'complete': True, 'scope': 'pinned_commit_and_all_reachable_ancestors',
                                 'commits_scanned': len(rows), 'pages_read': page, 'parent_graph_closed': True,
                                 'parent_graph_acyclic': True, 'ordering': 'API history order', 'message_only': True,
                                 'git_object_hashes_verified': False, 'global_search': False}}
    raise GVError('Commit history page bound reached; no partial search returned', 'pagination_limit')
