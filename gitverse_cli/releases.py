"""Release metadata and one-asset operations, with verified single mutations."""
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
from uuid import UUID

from .client import GVError
from .contents import resolve_ref, sha
from .issues import Session, page_result, write
from .metadata import same_metadata

FIELDS = ('id', 'tag_name', 'name', 'body', 'draft', 'prerelease', 'is_authorized_only', 'target_commitish')


def release_value(row, identity=None):
    if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0 or (identity is not None and row['id'] != identity) or not isinstance(row.get('tag_name'), str) or not row['tag_name'] or any(not isinstance(row.get(k), str) for k in ('name', 'body', 'target_commitish')) or any(type(row.get(k)) is not bool for k in ('draft', 'prerelease', 'is_authorized_only')):
        raise GVError('Invalid release identity/metadata', 'unexpected_response')
    return row


def same_release(a, b): return all(a.get(k) == b.get(k) for k in FIELDS)


def catalog(session, root):
    # Live API returns 404 rather than [] after the final release page.
    # Check repository reachability before interpreting this as a visible end.
    repository = session.request('GET', root)
    if not isinstance(repository, dict) or type(repository.get('id')) is not int or repository['id'] <= 0:
        raise GVError('Cannot confirm repository access for release listing', 'unexpected_response')
    rows, ids, tags = [], set(), set()
    for page in range(1, session.args.max_pages + 1):
        try: batch = session.request('GET', root + '/releases', {'page': page, 'per_page': 50})
        except GVError as error:
            if error.status != 404: raise
            after = session.request('GET', root)
            if not isinstance(after, dict) or after.get('id') != repository['id']: raise GVError('Repository access changed while listing releases', 'concurrent_change')
            return rows
        if not isinstance(batch, list): raise GVError('Unknown release catalog', 'unexpected_response')
        if not batch: return rows
        for row in batch:
            release_value(row)
            if row['id'] in ids or row['tag_name'] in tags: raise GVError('Repeated/shifting release catalog', 'pagination_error')
            ids.add(row['id']); tags.add(row['tag_name']); rows.append(row)
            if len(rows) > 100000: raise GVError('Release catalog entry bound reached', 'catalog_limit')
    raise GVError('Release page bound reached; no partial result returned', 'pagination_limit')


def find_release(session, root, tag):
    row = next((r for r in catalog(session, root) if r['tag_name'] == tag), None)
    if row is None: raise GVError('Release tag not found in catalog visible to token', 'release_not_found')
    current = release_value(session.request('GET', root + f"/releases/{row['id']}"), row['id'])
    if not same_release(current, row): raise GVError('Release changed during selection', 'concurrent_change')
    return current


def assets(session, path):
    rows = session.catalog(path + '/assets', size=50)
    seen = set()
    for row in rows:
        if not isinstance(row.get('name'), str) or not row['name'] or type(row.get('size')) is not int or row['size'] < 0 or not isinstance(row.get('uuid'), str):
            raise GVError('Invalid release asset metadata', 'unexpected_response')
        try:
            if str(UUID(row['uuid'])) != row['uuid']: raise ValueError()
        except ValueError as error: raise GVError('Invalid release asset UUID', 'unexpected_response') from error
        if row['name'] in seen: raise GVError('Ambiguous duplicate asset names', 'pagination_error')
        seen.add(row['name'])
    return rows


def same_asset(a, b): return all(a.get(k) == b.get(k) for k in ('id', 'uuid', 'name', 'size'))


def selected_asset(rows, name):
    row = next((r for r in rows if r['name'] == name), None)
    if row is None: raise GVError('Exact asset name not found', 'asset_not_found')
    return row


def asset_name(name):
    if not isinstance(name, str) or len(name) > 255 or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*\.[A-Za-z0-9_-]+', name):
        raise GVError('Asset name must be 1..255 ASCII letters/digits/_/-/dots with a file extension', 'usage_error')
    return name


def read_upload(path, maximum, deadline):
    if path.is_symlink(): raise GVError('Upload file must not be a symlink', 'usage_error')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode): raise GVError('Upload requires a regular file', 'usage_error')
        if before.st_size > maximum: raise GVError('Upload file exceeds byte bound', 'upload_limit')
        data = source.read(maximum + 1)
        after = os.fstat(source.fileno())
    if len(data) > maximum: raise GVError('Upload exceeds byte bound', 'upload_limit')
    if len(data) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino): raise GVError('Upload file changed while reading', 'concurrent_change')
    if time.monotonic() >= deadline: raise GVError('Upload read deadline reached', 'deadline_exceeded')
    return data


def release_command(client, repo, args):
    from .cli import body_text
    action, root = args.action, f'repos/{repo}'
    payload = {}
    if action != 'list' and (not args.tag or any(ord(c) < 32 or ord(c) == 127 for c in args.tag)):
        raise GVError('Use a non-empty exact tag without control characters', 'usage_error')
    if action in ('create', 'edit'):
        if args.title is not None:
            if not args.title.strip(): raise GVError('Release title must not be blank', 'usage_error')
            payload['name'] = args.title
        if args.body is not None or args.body_file is not None: payload['body'] = body_text(args)
        for key in ('draft', 'prerelease', 'is_authorized_only'):
            if getattr(args, key) is not None: payload[key] = getattr(args, key)
        if action == 'create':
            payload.update(tag_name=args.tag, target_commitish=args.target)
            payload.setdefault('name', args.tag); payload.setdefault('body', '')
            for key in ('draft', 'prerelease', 'is_authorized_only'): payload.setdefault(key, False)
        elif not payload: raise GVError('Specify release fields to edit', 'usage_error')
    local = Path(os.path.abspath(args.output)) if action == 'download' else None
    if local and (args.output == '-' or os.path.lexists(local)): raise GVError('Download requires a new output file', 'destination_exists')
    name = asset_name(Path(args.file).name if args.name is None else args.name) if action == 'upload' else getattr(args, 'name', None)
    if action in ('download', 'delete-asset') and (not name or any(ord(c) < 32 or ord(c) == 127 for c in name)): raise GVError('Use an exact asset name without controls', 'usage_error')
    if client.dry_run:
        return {'repository': repo, 'action': action, 'tag': getattr(args, 'tag', None), 'payload': payload,
                'asset_name': name, 'file': getattr(args, 'file', None), 'output': str(local) if local else None,
                'plan': 'Complete visible release/asset catalogs, select exact tag/name, precheck identities, one mutation and verify GET. Upload one bounded regular file; download one public UUID attachment to a new file. No redirects/network/files in dry-run.'}
    session = Session(client, args)
    if action == 'list':
        rows = catalog(session, root)
        selected = [r for r in rows if not (args.exclude_drafts and r['draft']) and not (args.exclude_pre_releases and r['prerelease'])]
        result = page_result(selected, 'releases', args, len(rows))
        result['coverage'].update(scope='visible_to_token', empty_page_404=True)
        return result
    if action == 'create':
        if any(r['tag_name'] == args.tag for r in catalog(session, root)): raise GVError('Release for tag already exists', 'release_exists')
        # Reuse bounded ref resolution; no automatic latest/default target.
        args.page_size, args.max_entries = 100, 100000
        _, commit = resolve_ref(client, repo, args.target, session.deadline, args)
        verified_target = session.request('GET', root + '/commits/' + commit)
        if not isinstance(verified_target, dict) or sha(verified_target.get('sha')) != commit:
            raise GVError('Resolved target commit was not confirmed', 'release_target_mismatch')
        payload['target_commitish'] = commit
        identity = {'repository': repo, 'tag': args.tag, 'release_id': None}
        def verify(result):
            if isinstance(result, dict) and type(result.get('id')) is int: identity['release_id'] = result['id']
            row = release_value(result)
            current = release_value(session.request('GET', root + f"/releases/{row['id']}"), row['id'])
            if not same_metadata(current, payload): raise GVError('Created release not confirmed', 'verification_failed')
            return {'release': current, 'changed': True, 'target_commit_sha': commit}
        return write(session, 'POST', root + '/releases', payload, verify, identity)
    before = find_release(session, root, args.tag)
    path = root + f"/releases/{before['id']}"
    identity = {'repository': repo, 'tag': args.tag, 'release_id': before['id']}
    if action == 'view': return before
    if action == 'edit' and payload.get('body') == '' and before['body'] != '':
        raise GVError('Public release PATCH ignores an empty body; clearing existing notes is unsupported', 'backend_unsupported')
    if action == 'edit' and same_metadata(before, payload): return {'release': before, 'changed': False}
    if action in ('edit', 'delete', 'upload', 'delete-asset'):
        latest = release_value(session.request('GET', path), before['id'])
        if not same_release(before, latest): raise GVError('Release changed before mutation', 'concurrent_change')
    if action == 'edit':
        def verify(result):
            current = release_value(session.request('GET', path), before['id'])
            if not same_metadata(current, payload): raise GVError('Release edit not confirmed', 'verification_failed')
            return {'release': current, 'changed': True}
        return write(session, 'PATCH', path, payload, verify, identity)
    if action == 'delete':
        def verify(result):
            try: session.request('GET', path)
            except GVError as error:
                if error.status == 404: return {**identity, 'deleted': True, 'tag_cleanup_requested': False}
                raise
            raise GVError('Release deletion not confirmed', 'verification_failed')
        return write(session, 'DELETE', path, None, verify, identity)
    rows = assets(session, path)
    if action == 'assets': return {**identity, 'assets': rows, 'coverage': {'catalog_complete': True}}
    if action == 'upload':
        if any(r['name'] == name for r in rows): raise GVError('Asset name already exists; no implicit overwrite', 'asset_exists')
        data = read_upload(Path(args.file), args.max_bytes, session.deadline)
        boundary = 'gv-' + secrets.token_hex(24)
        wire = (f'--{boundary}\r\nContent-Disposition: form-data; name="attachment"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + data + f'\r\n--{boundary}--\r\n'.encode())
        # Adapter keeps the shared mutation recovery path and exact one POST.
        class Upload:
            def request(self, method, endpoint, query=None, body=None):
                return client.request(method, endpoint, {'name': name}, encoded_body=wire, content_type='multipart/form-data; boundary=' + boundary, deadline=session.deadline, require_complete=True, max_response_bytes=1048576)
        def verify(result):
            if isinstance(result, dict) and type(result.get('id')) is int: identity['asset_id'] = result['id']
            current = selected_asset(assets(session, path), name)
            if not isinstance(result, dict) or current['id'] != result.get('id') or current['size'] != len(data): raise GVError('Uploaded asset metadata not confirmed', 'verification_failed')
            return {**identity, 'asset': current, 'sha256': hashlib.sha256(data).hexdigest(), 'verification': 'asset identity/name/size; SHA256 is local upload bytes'}
        return write(Upload(), 'POST', path + '/assets', None, verify, identity)
    selected = selected_asset(rows, name)
    identity['asset_id'] = selected['id']
    if action == 'delete-asset':
        current = selected_asset(assets(session, path), name)
        if not same_asset(selected, current): raise GVError('Asset changed before deletion', 'concurrent_change')
        def verify(result):
            if any(r['id'] == selected['id'] for r in assets(session, path)): raise GVError('Asset deletion not confirmed', 'verification_failed')
            return {**identity, 'name': name, 'deleted': True}
        return write(session, 'DELETE', path + f"/assets/{selected['id']}", None, verify, identity)
    if selected['size'] > args.max_bytes: raise GVError('Asset exceeds download byte bound', 'download_limit')
    public_path = 'attachments/' + selected['uuid']
    # Construct only the verified public route; browser/signed URLs are ignored.
    local.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.gv-release-', dir=local.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            info = client.download_to(public_path, output, args.max_bytes, session.deadline)
        if info['bytes'] != selected['size']: raise GVError('Downloaded asset size differs from metadata', 'incomplete_download')
        current = selected_asset(assets(session, path), name)
        after = release_value(session.request('GET', path), before['id'])
        if not same_asset(current, selected) or not same_release(before, after): raise GVError('Release/asset changed during download', 'concurrent_change')
        if time.monotonic() >= session.deadline: raise GVError('Download deadline reached', 'deadline_exceeded')
        try: os.link(temporary, local)
        except FileExistsError as error: raise GVError('Output appeared during download; preserved existing path', 'destination_exists') from error
        return {**identity, 'name': name, 'output': str(local), 'bytes_written': info['bytes'], 'sha256': info['sha256']}
    finally: os.unlink(temporary)
