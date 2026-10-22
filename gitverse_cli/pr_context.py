"""Bounded public review context with before/after PR identity checks."""
import re

from .checks import head_sha
from .client import GVError
from .issues import Session


def revision(pull, number):
    if not isinstance(pull, dict) or type(pull.get('id')) is not int or pull['id'] <= 0 or type(pull.get('number')) is not int or pull['number'] != number:
        raise GVError('Unexpected PR identity', 'unexpected_response')
    if not isinstance(pull.get('head'), dict):
        raise GVError('PR head metadata is missing', 'unexpected_response')
    head = head_sha(pull)
    base = pull.get('base')
    if not isinstance(base, dict) or not isinstance(base.get('sha'), str) or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', base['sha']):
        raise GVError('PR response has no valid base SHA', 'unexpected_response')
    if pull.get('state') not in ('open', 'closed') or type(pull.get('merged')) is not bool or not isinstance(pull.get('updated_at'), str) or not pull['updated_at']:
        raise GVError('PR state/version metadata is missing', 'unexpected_response')
    # Ignore unrelated mutable repository metadata in nested branch DTOs.
    def branch(value):
        repo = value.get('repo')
        return {k: value.get(k) for k in ('ref', 'label', 'repo_id')} | {
            'repository': {k: repo.get(k) for k in ('id', 'full_name')} if isinstance(repo, dict) else None}
    return {k: pull.get(k) for k in ('id', 'number', 'state', 'merged', 'updated_at', 'title', 'body', 'draft', 'changed_files')} | {
        'head_sha': head, 'base_sha': base['sha'].lower(), 'head': branch(pull['head']), 'base': branch(base)}


def collect_context(client, repo, args):
    path = f'repos/{repo}/pulls/{args.number}'
    routes = {'files': path + '/files', 'reviews': path + '/reviews',
              'comments': f'repos/{repo}/issues/{args.number}/comments', 'commits': path + '/commits'}
    if client.dry_run:
        return {'dry_run': True, 'repository': repo, 'number': args.number, 'method': 'GET',
                'paths': [path, *routes.values(), path],
                'plan': 'Read PR identity/base/head/version; complete bounded catalogs; comments use documented unpaginated endpoint; recheck PR. No network.'}
    session = Session(client, args)
    before = session.request('GET', path)
    pinned = revision(before, args.number)
    entries = 0
    coverage = {}

    def validate(rows, kind, seen):
        nonlocal entries
        if not isinstance(rows, list): raise GVError('Unknown PR context catalog shape', 'unexpected_response', details={'catalog': kind})
        for row in rows:
            if not isinstance(row, dict): raise GVError('Invalid PR context row', 'unexpected_response', details={'catalog': kind})
            key = row.get('filename' if kind == 'files' else 'sha' if kind == 'commits' else 'id')
            if kind == 'files': valid = isinstance(key, str) and bool(key)
            elif kind == 'commits': valid = isinstance(key, str) and re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', key)
            else: valid = type(key) is int and key > 0
            if not valid: raise GVError('Missing PR context row identity', 'unexpected_response', details={'catalog': kind})
            if kind == 'commits': key = key.lower()
            if key in seen: raise GVError('Duplicate or shifting PR context catalog', 'pagination_error', details={'catalog': kind})
            seen.add(key)
            entries += 1
            if entries > args.max_entries: raise GVError('Combined context entry bound reached; no partial result returned', 'context_limit')
            if kind == 'files' and row.get('patch') is not None and not isinstance(row['patch'], str):
                raise GVError('Unknown file patch shape', 'unexpected_response')

    result = {'pull_request': before}
    for kind, route in routes.items():
        rows, seen = [], set()
        if kind == 'comments':
            # The public schema documents no page/limit parameters for comments.
            batch = session.request('GET', route)
            validate(batch, kind, seen)
            rows = batch
            coverage[kind] = {'complete': True, 'mode': 'documented_unpaginated', 'requests': 1, 'entries': len(rows)}
        else:
            for page in range(1, args.max_pages + 1):
                batch = session.request('GET', route, {'page': page, 'limit' if kind == 'reviews' else 'per_page': args.page_size})
                validate(batch, kind, seen)
                if not batch:
                    coverage[kind] = {'complete': True, 'mode': 'paginated_to_empty', 'requests': page, 'entries': len(rows)}
                    break
                rows.extend(batch)
            else: raise GVError('Context page bound reached; no partial result returned', 'pagination_limit', details={'catalog': kind})
        result[kind] = rows
    after = session.request('GET', path)
    if revision(after, args.number) != pinned:
        raise GVError('PR changed while collecting context; request a fresh context before review', 'concurrent_change')
    count = before.get('changed_files')
    if type(count) is not int or count < 0:
        raise GVError('PR changed_files count is missing', 'unexpected_response')
    if count != len(result['files']):
        raise GVError('PR file count does not match complete catalog', 'incomplete_context')
    missing = [row['filename'] for row in result['files'] if not row.get('patch')]
    if args.require_patches and missing:
        raise GVError('Some files have no text patch; no context returned', 'incomplete_diff', details={'files': missing})
    result.update(repository=repo, number=args.number, head_sha=pinned['head_sha'], base_sha=pinned['base_sha'],
                  missing_patches=missing, coverage={'catalogs': coverage, 'complete': True, 'entries_scanned': entries,
                  'pr_revision_rechecked': True, 'atomic_snapshot': False, 'patch_integrity_verified': False,
                  'scope': 'Public PR files, commits, review summaries and discussion comments; no inline thread completeness claim'},
                  note='Use head_sha as pr review --commit. Missing patches may be binary, empty or truncated; present patches are not verified as full Git diffs. Concurrent discussion changes and changes reverted between reads cannot be excluded. Rich threads: pr threads --backend web.')
    return result
