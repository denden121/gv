"""Actions configuration with explicit scope, redacted secrets and verified writes."""
import os
from pathlib import Path
import re
import select
import stat
import sys
import time
from urllib.parse import quote

from .client import GVError
from .issues import Session, page_result


class _ConfigError(GVError):
    """Locally constructed errors whose text never contains value data."""


def config_scope(args):
    if args.org is not None:
        if getattr(args, 'repo', None): raise _ConfigError('Use --org or -R, not both', 'usage_error')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.org): raise _ConfigError('Use an organization login for --org', 'usage_error')
        return 'orgs/' + quote(args.org, safe='')
    from .cli import repository
    return 'repos/' + repository(args)


def metadata(row, kind, expected=None, include_value=False):
    if not isinstance(row, dict) or not isinstance(row.get('name'), str) or not row['name'] or any(ord(c) < 32 or ord(c) == 127 for c in row['name']) or (expected is not None and row['name'] != expected) or not isinstance(row.get('created_at'), str):
        raise _ConfigError('Invalid Actions configuration metadata', 'unexpected_response')
    keys = ('name', 'created_at') if kind == 'secret' else ('name', 'created_at', 'updated_at')
    if kind == 'variable' and (not isinstance(row.get('value'), str) or not isinstance(row.get('updated_at'), str)):
        raise _ConfigError('Invalid variable metadata', 'unexpected_response')
    result = {key: row[key] for key in keys}
    if kind == 'variable' and include_value: result['value'] = row['value']
    return result


def catalog(session, path, kind):
    result, seen, total = [], set(), None
    key = kind + 's'
    for page in range(1, session.args.max_pages + 1):
        data = session.request('GET', path, {'page': page, 'per_page': 100})
        if not isinstance(data, dict) or not isinstance(data.get(key), list) or type(data.get('total_count')) is not int or data['total_count'] < 0:
            raise _ConfigError('Unknown Actions catalog envelope', 'unexpected_response')
        if total is not None and data['total_count'] != total: raise _ConfigError('Actions catalog total changed', 'pagination_error')
        total = data['total_count']
        if total > 100000: raise _ConfigError('Actions catalog entry bound reached', 'catalog_limit')
        for row in data[key]:
            metadata(row, kind)
            if row['name'].casefold() in seen: raise _ConfigError('Repeated/ambiguous Actions names', 'pagination_error')
            seen.add(row['name'].casefold()); result.append(row)
        if len(result) > total: raise _ConfigError('Actions catalog count contradicts pages', 'pagination_error')
        if len(result) == total: return result
        if not data[key]: raise _ConfigError('Actions catalog ended before total_count', 'pagination_error')
    raise _ConfigError('Actions catalog page bound reached; no partial result returned', 'pagination_limit')


def read_value(args, deadline):
    maximum = args.max_bytes
    if args.value_file not in (None, '-'):
        path = Path(args.value_file)
        if path.is_symlink(): raise _ConfigError('Value file must be a regular file, not a symlink', 'usage_error')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(descriptor, 'rb') as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode): raise _ConfigError('Value file must be regular', 'usage_error')
            if before.st_size > maximum: raise _ConfigError('Value exceeds input byte bound', 'value_limit')
            data = source.read(maximum + 1)
            after = os.fstat(source.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns) or len(data) != before.st_size:
            raise _ConfigError('Value file changed while reading', 'concurrent_change')
    else:
        if sys.stdin.isatty(): raise _ConfigError('Pipe a value to stdin or supply --value-file; interactive input is not requested', 'usage_error')
        source = getattr(sys.stdin, 'buffer', sys.stdin)
        try: descriptor = source.fileno()
        except (AttributeError, OSError, ValueError): descriptor = None
        if descriptor is None:
            # In-memory streams used by embedding callers/tests have no fd.
            data = source.read(maximum + 1)
        else:
            chunks, size = [], 0
            while size <= maximum:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
                    raise _ConfigError('Stdin value deadline reached', 'deadline_exceeded')
                chunk = os.read(descriptor, min(65536, maximum - size + 1))
                if not chunk: break
                chunks.append(chunk); size += len(chunk)
            data = b''.join(chunks)
        if isinstance(data, str): data = data.encode('utf-8')
    if len(data) > maximum: raise _ConfigError('Value exceeds input byte bound', 'value_limit')
    try: value = data.decode('utf-8')
    except UnicodeError as error: raise _ConfigError('Value must be UTF-8 text; binary input is not implicitly encoded', 'usage_error') from error
    if '\0' in value: raise _ConfigError('Value must not contain NUL', 'usage_error')
    if args.group == 'secret' and not value: raise _ConfigError('Empty secret input refused; delete the secret explicitly to remove it', 'usage_error')
    if time.monotonic() >= deadline: raise _ConfigError('Value input deadline reached', 'deadline_exceeded')
    return value


def config_command(client, scope, args):
    kind, action = args.group, args.action
    name = getattr(args, 'name', None)
    if name is not None and not re.fullmatch(r'[A-Z_][A-Z0-9_]*', name):
        raise _ConfigError('Actions names must match [A-Z_][A-Z0-9_]*', 'usage_error')
    path = scope + '/actions/' + kind + 's'
    target = path + '/' + name if name else path
    context = {'scope': scope, 'kind': kind, 'action': action, 'name': name}
    if client.dry_run:
        return {**context, 'value': '<redacted>' if action == 'set' else None,
                'input': 'file' if getattr(args, 'value_file', None) not in (None, '-') else 'stdin',
                'plan': 'Read bounded catalog/metadata; variable set compares value before one POST/PATCH and verifies value readback. Secret PUT uses API query value over HTTPS, never prints it; metadata only verified. Delete verifies absence. No network/input reads/writes.'}
    session = Session(client, args)
    stage, attempted = 'preflight', False
    try:
        if action == 'list':
            rows = catalog(session, path, kind)
            clean = [metadata(row, kind, include_value=getattr(args, 'show_values', False)) for row in rows]
            return {**context, **page_result(clean, kind + 's', args, len(rows))}
        # A catalog distinguishes missing variables without assuming a GET 404
        # (the documented variable GET route only promises 200/500).
        rows = catalog(session, path, kind)
        selected = next((row for row in rows if row['name'] == name), None)
        if any(row['name'].casefold() == name.casefold() and row['name'] != name for row in rows):
            raise _ConfigError('Actions name differs only in case from an existing name', 'ambiguous_name')
        before = session.request('GET', target) if selected is not None else None
        if before is not None:
            metadata(before, kind, name)
            compared = ('name', 'created_at') if kind == 'secret' else ('name', 'created_at', 'updated_at', 'value')
            if any(before.get(k) != selected.get(k) for k in compared): raise _ConfigError('Actions entry changed during selection', 'concurrent_change')
        if action == 'get':
            if before is None: raise _ConfigError('Actions name not found in selected scope', 'config_not_found')
            return metadata(before, kind, name, include_value=kind == 'variable')
        if action == 'delete' and before is None:
            return {**context, 'deleted': False, 'changed': False, 'already_absent': True}
        value = read_value(args, session.deadline) if action == 'set' else None
        if kind == 'variable' and action == 'set' and before is not None and before['value'] == value:
            return {**context, 'variable': metadata(before, kind, name), 'changed': False, 'value_verified': True}
        if kind == 'variable' and action == 'set' and before is not None and value == '' and before['value'] != '':
            raise _ConfigError('Public variable PATCH ignores empty value; clearing existing values is unsupported', 'backend_unsupported')
        if before is not None:
            latest = session.request('GET', target)
            metadata(latest, kind, name)
            if any(latest.get(k) != before.get(k) for k in compared): raise _ConfigError('Actions entry changed before mutation', 'concurrent_change')
        stage, attempted = 'write', True
        if action == 'delete':
            session.request('DELETE', target)
        elif kind == 'secret':
            # This is the documented public GitVerse contract, not GitHub's
            # encrypted_value/public-key API. Never emit query/response value.
            session.request('PUT', target, {'value': value})
        else:
            session.request('PATCH' if before else 'POST', target if before else path, body={'name': name, 'value': value})
        stage = 'verify'
        after_rows = catalog(session, path, kind)
        found = next((row for row in after_rows if row['name'] == name), None)
        if action == 'delete':
            if found is not None: raise _ConfigError('Actions deletion not confirmed', 'verification_failed')
            return {**context, 'deleted': True, 'changed': True}
        if found is None: raise _ConfigError('Actions write metadata not confirmed', 'verification_failed')
        confirmed = session.request('GET', target)
        clean = metadata(confirmed, kind, name)
        if kind == 'secret':
            return {**context, 'secret': clean, 'write_accepted': True, 'metadata_verified': True, 'value_verified': False,
                    'verification': 'Secret API exposes no value; readback confirms metadata presence only'}
        if confirmed['value'] != value or found['value'] != value: raise _ConfigError('Variable value not confirmed', 'verification_failed')
        return {**context, 'variable': clean, 'changed': True, 'value_verified': True}
    except GVError as error:
        # Never forward upstream messages/details: a backend or proxy might
        # echo the query string, request body or a secret value.
        details = {**context, 'stage': stage, 'mutation_attempted': attempted,
                   'recovery': 'Read metadata/catalog before retrying. Secret value cannot be verified by API; do not blindly repeat writes.'}
        message = str(error) if isinstance(error, _ConfigError) else 'Actions configuration operation failed; values and upstream error details are redacted'
        raise GVError(message, error.code, error.status, details) from error
