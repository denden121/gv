"""Selected public repository settings, without rename/visibility/archive writes."""
from urllib.parse import urlsplit

from .checkout import valid_branch
from .client import GVError
from .issues import Session
from .repositories import identity


FIELDS = {'description': 'description', 'homepage': 'homepage', 'default_branch': 'default_branch',
          'allow_merge_commit': 'allow_merge', 'allow_squash_merge': 'allow_squash_merge',
          'allow_rebase_merge': 'allow_rebase',
          'is_template': 'is_template'}
OBSERVED = set(FIELDS) | {'delete_branch_on_merge'}
BOOLS = OBSERVED - {'description', 'homepage', 'default_branch'}


def requested(args):
    values = {key: getattr(args, key) for key in FIELDS if getattr(args, key, None) is not None}
    if not values: raise GVError('Pass at least one explicit repository setting', 'usage_error')
    for key in ('description', 'homepage'):
        if key not in values: continue
        value = values[key]
        if not isinstance(value, str) or len(value.encode('utf-8')) > 16384 or '\0' in value:
            raise GVError('Repository text must be UTF-8 without NUL, at most 16 KiB', 'usage_error')
    website = values.get('homepage')
    if website:
        try: url = urlsplit(website)
        except ValueError as error: raise GVError('Invalid homepage URL', 'usage_error') from error
        if url.scheme not in ('https', 'http') or not url.hostname or url.username is not None or url.password is not None or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in website):
            raise GVError('Homepage must be an HTTP(S) URL without credentials or whitespace', 'usage_error')
        try: url.port
        except ValueError as error: raise GVError('Invalid homepage port', 'usage_error') from error
    if 'default_branch' in values: valid_branch(values['default_branch'])
    return values


def snapshot(row, repo, keys):
    identity(row, repo)
    result = {}
    for key in keys:
        if key not in row: raise GVError('Repository setting is missing from response', 'unexpected_response')
        value = row[key]
        if key in BOOLS:
            if type(value) is not bool: raise GVError('Unknown repository boolean setting', 'unexpected_response')
        elif key in ('description', 'homepage'):
            if value is None: value = ''
            if not isinstance(value, str): raise GVError('Unknown repository text setting', 'unexpected_response')
        elif not isinstance(value, str) or not value:
            raise GVError('Unknown repository default branch', 'unexpected_response')
        result[key] = value
    return result


def branch_exists(session, path, branch):
    seen, found = set(), False
    for page in range(1, session.args.max_pages + 1):
        rows = session.request('GET', path + '/branches', {'page': page, 'per_page': 100})
        if not isinstance(rows, list): raise GVError('Unknown branch catalog', 'unexpected_response')
        if not rows:
            if not found: raise GVError('Default branch must match an existing branch', 'branch_not_found')
            return
        for row in rows:
            name = row.get('name') if isinstance(row, dict) else None
            if not isinstance(name, str) or not name or name in seen:
                raise GVError('Invalid or repeated branch catalog', 'pagination_error')
            seen.add(name)
            if len(seen) > 100000: raise GVError('Branch catalog bound reached', 'catalog_limit')
            if name == branch: found = True
    raise GVError('Branch page bound reached; no write attempted', 'pagination_limit')


def edit_repository(client, repo, args):
    values = requested(args)
    payload = {FIELDS[k]: v for k, v in values.items()}
    path = 'repos/' + repo
    if client.dry_run:
        return {'dry_run': True, 'repository': repo, 'method': 'PATCH', 'path': path, 'body': payload,
                'plan': 'Read settings, verify existing default branch, skip equal values, recheck requested fields, PATCH once, verify GET. No network.'}
    session = Session(client, args)
    before = session.request('GET', path)
    before_settings = snapshot(before, repo, OBSERVED)
    old = {k: before_settings[k] for k in values}
    changes = {k: v for k, v in values.items() if old[k] != v}
    if not changes:
        return {'repository': repo, 'repository_id': before['id'], 'changed': False, 'settings': old,
                'verification': {'readback': True, 'atomic_compare_and_swap': False}}
    if 'default_branch' in changes: branch_exists(session, path, changes['default_branch'])
    current = session.request('GET', path)
    current_settings = snapshot(current, repo, OBSERVED)
    if {k: current_settings[k] for k in values} != old or current['id'] != before['id'] or current['private'] != before['private']:
        raise GVError('Requested repository settings changed during preflight; reread before retry', 'concurrent_change',
                      details={'repository': repo, 'mutation_attempted': False})
    stage = 'write'
    try:
        session.request('PATCH', path, body={FIELDS[k]: v for k, v in changes.items()})
        stage = 'verify'
        after = session.request('GET', path)
        after_settings = snapshot(after, repo, OBSERVED)
        actual = {k: after_settings[k] for k in values}
        if after['id'] != before['id'] or after['private'] != before['private'] or after_settings != {**current_settings, **values}:
            raise GVError('Repository settings readback does not match requested values', 'verification_failed')
        return {'repository': repo, 'repository_id': before['id'], 'changed': True, 'changed_fields': list(changes),
                'settings': actual, 'verification': {'readback': True, 'atomic_compare_and_swap': False}}
    except GVError as error:
        error.details = {'repository': repo, 'repository_id': before['id'], 'stage': stage,
                         'mutation_attempted': True, 'requested_fields': list(changes),
                         'recovery': 'Read repo view before retrying; no automatic rollback or mutation retry'}
        raise
