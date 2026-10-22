"""Bounded literal searches; coverage describes the explicitly scanned scope."""
import re
import time
from urllib.parse import quote

from .client import GVError
from .contents import get, path_name, read_blob, resolve_ref, tree


def prepare(args):
    if not args.query or not args.query.strip() or any(c in args.query for c in ('\r', '\n', '\0')):
        raise GVError('Query must be a non-blank single-line literal without NUL', 'usage_error')
    if args.page_size > 100: raise GVError('--page-size must not exceed 100', 'usage_error')
    return args.query if args.case_sensitive else args.query.casefold()


def matches(text, needle, args):
    return needle in (text if args.case_sensitive else text.casefold())


def search_repos(client, args):
    needle = prepare(args)
    owner = args.owner
    if owner != '@me' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', owner):
        raise GVError('Use @me or a user login for --owner', 'usage_error')
    scope = 'authenticated_user_owned' if owner == '@me' else 'user_public'
    path = 'user/repos' if owner == '@me' else f'users/{quote(owner, safe="")}/repos'
    base = {'query': args.query, 'owner': owner, 'scope': scope, 'case_sensitive': args.case_sensitive}
    if client.dry_run:
        return {**base, 'plan': 'Fully scan public API repository catalog to empty page; literal name/full_name/description match. No global site search.'}
    deadline = time.monotonic() + args.timeout
    seen, names, results = set(), set(), []
    for page in range(1, args.max_pages + 1):
        values = get(client, path, {'page': page, 'per_page': args.page_size}, deadline)
        if not isinstance(values, list): raise GVError('Unknown repository catalog', 'unexpected_response')
        for value in values:
            if not isinstance(value, dict) or type(value.get('id')) is not int or value['id'] <= 0 or not isinstance(value.get('name'), str) or not isinstance(value.get('full_name'), str) or not value['name'] or not value['full_name'] or type(value.get('private')) is not bool or (value.get('description') is not None and not isinstance(value['description'], str)):
                raise GVError('Invalid repository search metadata', 'unexpected_response')
            if value['id'] in seen or value['full_name'].casefold() in names:
                raise GVError('Repeated or shifting repository pages', 'pagination_error')
            seen.add(value['id']); names.add(value['full_name'].casefold())
            if len(seen) > args.max_entries: raise GVError('Repository catalog entry bound reached', 'search_limit')
            if any(matches(value.get(key) or '', needle, args) for key in ('name', 'full_name', 'description')):
                results.append({key: value.get(key) for key in ('id', 'name', 'full_name', 'description', 'private', 'html_url', 'default_branch')})
        if not values:
            return {**base, 'results': results, 'total_matches': len(results),
                    'coverage': {'complete': True, 'repositories_scanned': len(seen), 'atomic_snapshot': False}}
    raise GVError('Repository catalog page bound reached; no partial search returned', 'pagination_limit')


def search_code(client, repo, args):
    needle = prepare(args)
    path = path_name(args.path)
    base = {'repository': repo, 'query': args.query, 'path': path, 'case_sensitive': args.case_sensitive}
    if client.dry_run:
        return {**base, 'ref': args.ref or '<default-branch>', 'plan': 'Pin ref, scan full recursive tree, read/hash every regular blob in path scope; literal UTF-8 line matches, binary/special skips disclosed. Bounds fail without partial results.'}
    deadline = time.monotonic() + args.timeout
    ref, commit = resolve_ref(client, repo, args.ref, deadline, args)
    entries = tree(client, repo, commit, args, deadline)
    if path and not any(row['path'] == path for row in entries): raise GVError('Search path missing in pinned commit', 'path_not_found')
    scoped = [row for row in entries if not path or row['path'] == path or row['path'].startswith(path + '/')]
    files = [row for row in scoped if row['type'] == 'file']
    if len(files) > args.max_files or sum(row['size'] for row in files) > args.max_total_bytes or any(row['size'] > args.max_bytes for row in files):
        raise GVError('Search file/byte bound reached; narrow --path or raise explicit limits', 'search_limit')
    results, binary, special, total_bytes, text_count = [], [], [], 0, 0
    for row in scoped:
        if row['type'] in ('symlink', 'submodule'): special.append({'path': row['path'], 'type': row['type']})
    for row in files:
        data = read_blob(client, repo, commit, row, args.max_bytes, deadline)
        total_bytes += len(data)
        try:
            text = data.decode('utf-8')
            if '\0' in text: raise UnicodeError()
        except UnicodeError:
            binary.append(row['path']); continue
        text_count += 1
        # Only CR/LF delimit source lines; Unicode separators remain content.
        for number, line in enumerate(re.split(r'\r\n|\r|\n', text), 1):
            if matches(line, needle, args):
                results.append({'path': row['path'], 'blob_sha': row['sha'], 'line_number': number, 'text': line})
                if len(results) > args.max_matches: raise GVError('Search match bound reached; no partial result returned', 'search_limit')
        if time.monotonic() >= deadline: raise GVError('Code search deadline reached', 'deadline_exceeded')
    return {**base, 'ref': ref, 'commit_sha': commit, 'results': results, 'total_matches': len(results),
            'coverage': {'complete': True, 'scope': 'regular_utf8_file_lines', 'tree_entries_scanned': len(entries),
                         'files_read': len(files), 'text_files_searched': text_count, 'bytes_read': total_bytes,
                         'binary_files_skipped': binary, 'special_entries_skipped': special,
                         'lfs': 'Git pointer text only; LFS objects are not downloaded'}}
