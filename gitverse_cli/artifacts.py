"""Bounded artifact downloads into private staging; safe ZIP extraction."""
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import tempfile
import time
import unicodedata
import zipfile
import zlib

from .client import GVError
from .run_watch import commit


def artifact(row):
    if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0 or not isinstance(row.get('name'), str) or not row['name'] or type(row.get('expired')) is not bool:
        raise GVError('Incomplete artifact metadata', 'unexpected_response')
    run = row.get('workflow_run')
    if not isinstance(run, dict) or type(run.get('id')) is not int or run['id'] <= 0 or not isinstance(run.get('head_sha'), str):
        raise GVError('Artifact run association is missing', 'unexpected_response')
    return row


def catalog(client, repo, args, deadline):
    rows, seen, total = [], set(), None
    for page in range(1, args.max_pages + 1):
        data = client.request('GET', f'repos/{repo}/actions/artifacts', {'page': page, 'per_page': 50}, deadline=deadline)
        if not isinstance(data, dict) or not isinstance(data.get('artifacts'), list):
            raise GVError('Unknown artifact list response', 'unexpected_response')
        count = data.get('total_count')
        if count is not None:
            if type(count) is not int or count < 0 or (total is not None and count != total):
                raise GVError('Invalid or shifting artifact count', 'pagination_error')
            total = count
        for row in data['artifacts']:
            artifact(row)
            if row['id'] in seen: raise GVError('Repeated or shifting artifact pages', 'pagination_error')
            seen.add(row['id']); rows.append(row)
        if total is not None and len(rows) > total: raise GVError('Artifact count contradicts pages', 'pagination_error')
        if total is not None and len(rows) == total: return rows
        if not data['artifacts']:
            if total is not None: raise GVError('Artifact pages ended before total_count', 'pagination_error')
            return rows
    raise GVError('Artifact pagination bound reached', 'pagination_limit')


def portable_parts(value):
    if not isinstance(value, str) or not value or value.startswith('/') or '\\' in value or ':' in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise GVError('Unsafe archive path', 'unsafe_archive')
    parts = value.rstrip('/').split('/')
    for part in parts:
        if part in ('', '.', '..') or part.endswith((' ', '.')) or re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part):
            raise GVError('Unsafe or non-portable archive path', 'unsafe_archive')
    return parts


def extract(archive, root, args, budget, deadline):
    seen, spellings, plan = {}, {}, []
    entries = archive.infolist()
    budget['files'] += len(entries)
    if budget['files'] > args.max_files: raise GVError('ZIP entry limit exceeded', 'download_limit')
    for info in entries:
        if time.monotonic() >= deadline: raise GVError('Extraction deadline reached', 'deadline_exceeded')
        parts = portable_parts(info.orig_filename)
        mode = stat.S_IFMT(info.external_attr >> 16)
        if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 1:
            raise GVError('Symlinks, special files and encrypted ZIP entries are refused', 'unsafe_archive')
        if mode == stat.S_IFDIR and not info.is_dir():
            raise GVError('Inconsistent archive entry type', 'unsafe_archive')
        keys = tuple(unicodedata.normalize('NFC', p).casefold() for p in parts)
        for length in range(1, len(keys) + 1):
            key, spelling = keys[:length], tuple(parts[:length])
            if key in spellings and spellings[key] != spelling:
                raise GVError('Case/Unicode-colliding archive directories', 'unsafe_archive')
            spellings[key] = spelling
        for length in range(1, len(keys)):
            prefix = keys[:length]
            if seen.get(prefix) == 'file': raise GVError('Archive file/directory collision', 'unsafe_archive')
            seen[prefix] = 'dir'
        kind = 'dir' if info.is_dir() else 'file'
        if keys in seen and not (kind == seen[keys] == 'dir'):
            raise GVError('Duplicate or case/Unicode-colliding archive paths', 'unsafe_archive')
        seen[keys] = kind
        budget['expanded'] += info.file_size
        if budget['expanded'] > args.max_extracted_bytes: raise GVError('Uncompressed size limit exceeded', 'download_limit')
        plan.append((info, root.joinpath(*parts)))
    for info, target in plan:
        if info.is_dir(): target.mkdir(parents=True, exist_ok=True); continue
        target.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with archive.open(info) as source, target.open('xb') as output:
            while True:
                if time.monotonic() >= deadline: raise GVError('Extraction deadline reached', 'deadline_exceeded')
                chunk = source.read(65536)
                if not chunk: break
                size += len(chunk)
                if size > info.file_size: raise GVError('Uncompressed size differs from ZIP metadata', 'unsafe_archive')
                output.write(chunk)
        if size != info.file_size: raise GVError('Truncated ZIP entry', 'unsafe_archive')


def unchanged(before, after):
    return all(before.get(key) == after.get(key) for key in ('id', 'name', 'expired', 'size_in_bytes', 'updated_at', 'workflow_run'))


def preflight_zip(path, remaining_files, deadline):
    """Count central records before ZipFile allocates one object per entry."""
    size = path.stat().st_size
    with path.open('rb') as source:
        tail_start = max(0, size - 65557)
        source.seek(tail_start); tail = source.read()
        marker = tail.rfind(b'PK\x05\x06')
        if marker < 0 or len(tail) - marker < 22: raise GVError('Missing ZIP end record', 'unsafe_archive')
        _, disk, central_disk, disk_count, count, central_size, offset, comment = struct.unpack('<4s4H2LH', tail[marker:marker + 22])
        if marker + 22 + comment != len(tail) or disk or central_disk or disk_count != count or count == 65535 or offset == 4294967295 or central_size == 4294967295:
            raise GVError('Multi-volume, ZIP64 end records or malformed ZIP are unsupported', 'unsafe_archive')
        if count > remaining_files: raise GVError('ZIP entry limit exceeded', 'download_limit')
        if offset + central_size != tail_start + marker: raise GVError('Invalid ZIP central directory bounds', 'unsafe_archive')
        source.seek(offset); end, actual = offset + central_size, 0
        while source.tell() < end:
            if time.monotonic() >= deadline: raise GVError('ZIP validation deadline reached', 'deadline_exceeded')
            header = source.read(46)
            if len(header) != 46 or header[:4] != b'PK\x01\x02': raise GVError('Invalid ZIP central directory', 'unsafe_archive')
            actual += 1
            if actual > remaining_files: raise GVError('ZIP entry limit exceeded', 'download_limit')
            name, extra, note = struct.unpack('<3H', header[28:34])
            source.seek(name + extra + note, 1)
        if source.tell() != end or actual != count: raise GVError('ZIP entry count contradicts directory', 'unsafe_archive')
        return actual


def download_artifacts(client, repo, args):
    destination = Path(os.path.abspath(args.dir or f'artifacts-{args.run_id}'))
    if os.path.lexists(destination): raise GVError('Destination already exists; choose a new directory', 'destination_exists')
    names = set(args.name or [])
    if any(not n or any(ord(c) < 32 or ord(c) == 127 for c in n) for n in names):
        raise GVError('Artifact names must be non-empty without control characters', 'usage_error')
    if client.dry_run:
        return {'run_request': client.request('GET', f'repos/{repo}/actions/runs/{args.run_id}'),
                'artifacts_request': client.request('GET', f'repos/{repo}/actions/artifacts', {'page': 1, 'per_page': 50}),
                'destination': str(destination), 'names': sorted(names), 'archive': args.archive,
                'plan': 'Read complete repository catalog, match run ID/SHA and exact names, download same-origin /zip/raw with limits, verify metadata/ZIP and stage locally; publish into a new directory. No network or writes in dry-run.'}
    deadline = time.monotonic() + args.timeout
    run_path = f'repos/{repo}/actions/runs/{args.run_id}'
    run = client.request('GET', run_path, deadline=deadline)
    if not isinstance(run, dict) or type(run.get('id')) is not int or run['id'] != args.run_id: raise GVError('Unexpected run identity', 'unexpected_response')
    sha = commit(run)
    selected = [r for r in catalog(client, repo, args, deadline) if r['workflow_run']['id'] == args.run_id and (not names or r['name'] in names)]
    missing = names - {r['name'] for r in selected}
    if missing: raise GVError('Requested artifact names were not found in this run', 'artifact_not_found', details={'names': sorted(missing)})
    if not selected: raise GVError('No artifacts found for this run', 'artifact_not_found')
    if any(r['expired'] for r in selected): raise GVError('Selected artifacts have expired', 'artifact_expired')
    if any(r['workflow_run']['head_sha'].lower() != sha for r in selected):
        raise GVError('Artifact SHA does not match requested run', 'artifact_run_mismatch')
    destination.parent.mkdir(parents=True, exist_ok=True)
    budget = {'compressed': 0, 'expanded': 0, 'files': 0}
    result = []
    with tempfile.TemporaryDirectory(prefix='.gv-download-', dir=destination.parent) as temporary:
        stage = Path(temporary); payload = stage / 'payload'; payload.mkdir(mode=0o700)
        for row in selected:
            path = f"repos/{repo}/actions/artifacts/{row['id']}"
            before = artifact(client.request('GET', path, deadline=deadline))
            if not unchanged(row, before): raise GVError('Artifact metadata changed before download', 'artifact_changed')
            archive_path = stage / f"{row['id']}.zip"
            with archive_path.open('xb') as output:
                info = client.download_to(path + '/zip/raw', output, args.max_bytes - budget['compressed'], deadline)
            budget['compressed'] += info['bytes']
            after = artifact(client.request('GET', path, deadline=deadline))
            if not unchanged(before, after): raise GVError('Artifact metadata changed during download', 'artifact_changed')
            try:
                count = preflight_zip(archive_path, args.max_files - budget['files'], deadline)
                with zipfile.ZipFile(archive_path) as archive:
                    if args.archive:
                        # Archive mode validates the ZIP directory, without extraction.
                        budget['files'] += count
                        subdirectory = None
                    else:
                        subdirectory = None if len(selected) == 1 else str(row['id'])
                        root = payload if subdirectory is None else payload / subdirectory
                        root.mkdir(exist_ok=True)
                        extract(archive, root, args, budget, deadline)
                if args.archive: shutil.move(archive_path, payload / f"{row['id']}.zip")
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error) as error:
                raise GVError('Invalid, corrupt or unsupported artifact ZIP; destination was not published', 'unsafe_archive') from error
            result.append({'id': row['id'], 'name': row['name'], 'sha256': info['sha256'], 'compressed_bytes': info['bytes'],
                           'path': str(destination / (f"{row['id']}.zip" if args.archive else (subdirectory or '')))})
        current = client.request('GET', run_path, deadline=deadline)
        if not isinstance(current, dict) or type(current.get('id')) is not int or current['id'] != args.run_id or commit(current) != sha:
            raise GVError('Run commit changed while downloading artifacts', 'artifact_run_mismatch')
        if time.monotonic() >= deadline: raise GVError('Download deadline reached', 'deadline_exceeded')
        if os.path.lexists(destination): raise GVError('Destination appeared during download', 'destination_exists')
        os.rename(payload, destination)
    return {'repository': repo, 'run_id': args.run_id, 'commit_sha': sha, 'directory': str(destination),
            'mode': 'archive' if args.archive else 'extract', 'artifacts': result, 'totals': budget}
