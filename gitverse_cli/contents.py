"""Pinned file/directory reads with complete trees and verified Git blob bytes."""
import base64
import binascii
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import quote

from .client import GVError


@dataclass
class RawContents:
    data: bytes


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', value):
        raise GVError('Missing or invalid Git object SHA', 'unexpected_response')
    return value.lower()


def path_name(value):
    if value in ('', '.'): return ''
    if value.startswith('/') or '\\' in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise GVError('Use a repository-relative path without control characters', 'usage_error')
    value = value.removesuffix('/')
    if any(part in ('', '.', '..') for part in value.split('/')):
        raise GVError('Repository paths must not contain traversal or empty components', 'usage_error')
    return value


def get(client, path, query, deadline, maximum=33554432):
    return client.request('GET', path, query, deadline=deadline, require_complete=True, max_response_bytes=maximum)


def resolve_ref(client, repo, ref, deadline, args):
    if ref is not None and (not ref or any(ord(c) < 32 or ord(c) == 127 for c in ref)):
        raise GVError('Ref must be non-empty without control characters', 'usage_error')
    if ref is not None and re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', ref): return ref, ref.lower()
    if ref is None:
        data = get(client, f'repos/{repo}', None, deadline)
        ref = data.get('default_branch') if isinstance(data, dict) else None
        if not isinstance(ref, str) or not ref: raise GVError('Unknown repository default branch', 'unexpected_response')
    if '/' in ref:
        # Public API validators reject slash refs even when URL-encoded or
        # passed as commits?sha=. Branch lists expose exact names and heads.
        branch = ref.removeprefix('refs/heads/')
        seen, found = set(), None
        for page in range(1, args.max_pages + 1):
            values = get(client, f'repos/{repo}/branches', {'page': page, 'per_page': args.page_size}, deadline)
            if not isinstance(values, list): raise GVError('Unknown branch list response', 'unexpected_response')
            for value in values:
                name = value.get('name') if isinstance(value, dict) else None
                if not isinstance(name, str) or not name or name in seen: raise GVError('Repeated or invalid branch pages', 'pagination_error')
                seen.add(name)
                if len(seen) > args.max_entries: raise GVError('Branch lookup entry limit reached', 'contents_limit')
                details = value.get('commit')
                head = sha(details.get('sha') if isinstance(details, dict) else None)
                if name == branch: found = head
            if not values:
                if found is None: raise GVError('Slash ref did not match a branch; slash-containing tags require full commit SHA', 'ref_not_found')
                return ref, found
        raise GVError('Branch lookup pagination bound reached', 'pagination_limit')
    data = get(client, f'repos/{repo}/commits/{quote(ref, safe="")}', None, deadline)
    return ref, sha(data.get('sha') if isinstance(data, dict) else None)


def entry(row):
    if not isinstance(row, dict) or not isinstance(row.get('path'), str) or not row['path'] or path_name(row['path']) != row['path']:
        raise GVError('Invalid Git tree path', 'unexpected_response')
    sha(row.get('sha'))
    kinds = {'040000': ('tree', 'dir'), '40000': ('tree', 'dir'), '100644': ('blob', 'file'),
             '100755': ('blob', 'file'), '120000': ('blob', 'symlink'), '160000': ('commit', 'submodule')}
    mode = row.get('mode')
    if not isinstance(mode, str) or mode not in kinds or row.get('type') != kinds[mode][0]: raise GVError('Unknown or inconsistent Git entry mode/type', 'unexpected_response')
    kind = kinds[mode][1]
    if kind == 'file' and (type(row.get('size')) is not int or row['size'] < 0): raise GVError('Missing file size', 'unexpected_response')
    return {'path': row['path'], 'name': row['path'].split('/')[-1], 'type': kind, 'mode': mode,
            'sha': row['sha'].lower(), 'size': row.get('size')}


def tree(client, repo, commit, args, deadline):
    rows, seen, count = [], set(), None
    for page in range(1, args.max_pages + 1):
        data = get(client, f'repos/{repo}/git/trees/{commit}', {'page': page, 'per_page': args.page_size, 'recursive': 'true'}, deadline)
        if not isinstance(data, dict) or sha(data.get('sha')) != commit or not isinstance(data.get('tree'), list) or type(data.get('truncated')) is not bool or type(data.get('page')) is not int or data['page'] != page:
            raise GVError('Invalid Git tree response/identity/page', 'unexpected_response')
        total = data.get('total_count')
        if type(total) is not int or total < 0 or (count is not None and count != total): raise GVError('Invalid or changing Git tree count', 'pagination_error')
        count = total
        if total > args.max_entries: raise GVError('Full repository tree exceeds entry limit', 'contents_limit')
        batch = data['tree']
        for row in batch:
            item = entry(row)
            if item['path'] in seen: raise GVError('Repeated or shifting tree pages', 'pagination_error')
            seen.add(item['path']); rows.append(item)
        if len(rows) > total: raise GVError('Tree count contradicts pages', 'pagination_error')
        if not data['truncated']:
            if len(rows) != total: raise GVError('Tree ended before total_count', 'pagination_error')
            # Recursive trees must include every parent directory.
            directories = {r['path'] for r in rows if r['type'] == 'dir'}
            for row in rows:
                parts = row['path'].split('/')
                if any('/'.join(parts[:n]) not in directories for n in range(1, len(parts))):
                    raise GVError('Recursive tree is missing a parent directory', 'incomplete_response')
            return rows
        if not batch or len(rows) >= total: raise GVError('Tree truncation contradicts count/pages', 'pagination_error')
    raise GVError('Git tree pagination bound reached; no partial result returned', 'pagination_limit')


def save_file(path, data, deadline):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.gv-file-', dir=path.parent)
    try:
        with os.fdopen(handle, 'wb') as output: output.write(data)
        if time.monotonic() >= deadline: raise GVError('File output deadline reached', 'deadline_exceeded')
        try: os.link(temporary, path)
        except FileExistsError as error: raise GVError('Output path already exists; choose a new file', 'destination_exists') from error
    finally:
        os.unlink(temporary)


def read_contents(client, repo, args):
    path = path_name(args.path)
    if args.page_size > 100: raise GVError('--page-size must be between 1 and 100', 'usage_error')
    raw, output = getattr(args, 'raw', False), getattr(args, 'output', None)
    if raw and getattr(args, 'fields', None): raise GVError('--raw cannot be combined with --fields', 'usage_error')
    if args.action == 'read-file' and not path: raise GVError('read-file requires a file path', 'usage_error')
    if output == '-': raise GVError('Use --raw for stdout; --output requires a new file path', 'usage_error')
    local = Path(os.path.abspath(output)) if output else None
    if local and os.path.lexists(local): raise GVError('Output path already exists; choose a new file', 'destination_exists')
    if args.ref is not None and (not args.ref or any(ord(c) < 32 or ord(c) == 127 for c in args.ref)):
        raise GVError('Ref must be non-empty without control characters', 'usage_error')
    if client.dry_run:
        return {'repository': repo, 'path': path, 'ref': args.ref or '<default-branch>', 'output': str(local) if local else None,
                'plan': 'Resolve ref to commit SHA, fully paginate recursive Git tree, verify type/path. For files: fetch base64 at pinned SHA, verify size and Git blob hash; JSON UTF-8/base64 or explicit exact bytes/new file. No network/writes.'}
    deadline = time.monotonic() + args.timeout
    ref, commit = resolve_ref(client, repo, args.ref, deadline, args)
    rows = tree(client, repo, commit, args, deadline)
    selected = next((r for r in rows if r['path'] == path), None) if path else {'path': '', 'type': 'dir'}
    if selected is None: raise GVError('Path does not exist in the pinned commit', 'path_not_found')
    if args.action == 'read-dir' and selected['type'] != 'dir': raise GVError('Requested path is not a directory', 'contents_type_error')
    if args.action == 'read-file' and selected['type'] != 'file': raise GVError('read-file accepts regular blobs only; symlinks/submodules are not followed', 'contents_type_error')
    base = {'repository': repo, 'path': path, 'ref': ref, 'commit_sha': commit}
    if selected['type'] == 'dir':
        prefix = path + '/' if path else ''
        recursive = getattr(args, 'recursive', False)
        entries = [r for r in rows if r['path'].startswith(prefix) and r['path'] != path and (recursive or '/' not in r['path'][len(prefix):])]
        return {**base, 'type': 'dir', 'recursive': recursive, 'entries': entries,
                'coverage': {'complete': True, 'tree_entries_scanned': len(rows)}}
    if selected['type'] != 'file': return {**base, **selected, 'content_omitted': 'symlinks/submodules are metadata-only and never followed'}
    if getattr(args, 'recursive', False): raise GVError('--recursive requires a directory', 'usage_error')
    binary = read_blob(client, repo, commit, selected, args.max_bytes, deadline)
    if raw: return RawContents(binary)
    summary = {**base, **selected, 'sha256': hashlib.sha256(binary).hexdigest()}
    if local:
        save_file(local, binary, deadline)
        return {**summary, 'output': str(local), 'bytes_written': len(binary)}
    try:
        text = binary.decode('utf-8')
        if '\0' in text: raise UnicodeError()
    except UnicodeError:
        return {**summary, 'binary': True, 'encoding': 'base64', 'content': base64.b64encode(binary).decode('ascii')}
    return {**summary, 'binary': False, 'encoding': 'utf-8', 'content': text}


def read_blob(client, repo, commit, selected, max_bytes, deadline):
    path = selected['path']
    if selected['size'] > max_bytes: raise GVError('File exceeds decoded byte limit', 'contents_limit')
    encoded_path = '/'.join(quote(part, safe='') for part in path.split('/'))
    data = get(client, f'repos/{repo}/contents/{encoded_path}', {'ref': commit, 'scope': 'fast'}, deadline, (max_bytes * 4 // 3) + 262144)
    if not isinstance(data, dict) or data.get('type') != 'file' or data.get('path') != path or data.get('encoding') != 'base64' or not isinstance(data.get('sha'), str) or data['sha'].lower() != selected['sha'] or type(data.get('size')) is not int or data['size'] != selected['size'] or not (isinstance(data.get('content'), str) or (selected['size'] == 0 and data.get('content') is None)):
        raise GVError('File content metadata is missing, changed or incomplete', 'incomplete_response')
    # GitVerse returns null or omits content for an empty blob. Size and Git
    # blob hash still independently prove that its exact content is b''.
    try: binary = base64.b64decode(''.join((data.get('content') or '').split()), validate=True)
    except (ValueError, binascii.Error) as error: raise GVError('Invalid base64 file content', 'unexpected_response') from error
    if len(binary) != selected['size'] or len(binary) > max_bytes: raise GVError('Decoded file size is incomplete or exceeds limit', 'incomplete_response')
    digest = hashlib.sha1 if len(selected['sha']) == 40 else hashlib.sha256
    if digest(b'blob ' + str(len(binary)).encode() + b'\0' + binary).hexdigest() != selected['sha']:
        raise GVError('File content does not match its Git blob SHA', 'blob_mismatch')
    if time.monotonic() >= deadline: raise GVError('Contents deadline reached', 'deadline_exceeded')
    return binary
