"""Complete bounded run catalogs with explicit, locally verified filters."""
import re
from urllib.parse import quote

from .checks import bucket
from .client import GVError
from .issues import Session, page_result
from .workflows import clean, filename, workflow_row


def catalog(session, path, key, validate, identity):
    rows, seen, expected = [], set(), None
    for page in range(1, session.args.max_pages + 1):
        data = session.request('GET', path, {'page': page, 'per_page': 100})
        if not isinstance(data, dict) or not isinstance(data.get(key), list):
            raise GVError('Unknown CI catalog envelope', 'unexpected_response')
        total = data.get('total_count')
        if total is not None:
            if type(total) is not int or total < 0 or (expected is not None and total != expected):
                raise GVError('Invalid or changing CI catalog count', 'pagination_error')
            expected = total
        batch = data[key]
        for row in batch:
            validate(row)
            value = identity(row)
            if value in seen: raise GVError('Repeated or shifting CI catalog pages', 'pagination_error')
            seen.add(value); rows.append(row)
            if len(rows) > 100000: raise GVError('CI catalog entry bound reached', 'catalog_limit')
        if expected is not None:
            if len(rows) > expected: raise GVError('CI catalog contradicts total_count', 'pagination_error')
            if len(rows) == expected: return rows
        if not batch:
            if expected is not None: raise GVError('CI catalog ended before total_count', 'pagination_error')
            return rows
    raise GVError('CI catalog page bound reached; no partial result returned', 'pagination_limit')


def run_row(row):
    if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0:
        raise GVError('CI catalog contains an invalid run identity', 'unexpected_response')
    return row


def text(row, key):
    value = row.get(key)
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise GVError('Run lacks valid metadata required by filter', 'unexpected_response', details={'run_id': row['id'], 'field': key})
    return value


def filters(args):
    result = {}
    for key in ('branch', 'event', 'status', 'user', 'commit', 'workflow'):
        value = getattr(args, key, None)
        if value is not None: result[key] = clean(value, key)
    if 'commit' in result:
        if not re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', result['commit']):
            raise GVError('Use a full 40/64 hexadecimal commit SHA, not a prefix', 'usage_error')
        result['commit'] = result['commit'].lower()
    if 'branch' in result:
        branch = result['branch'].removeprefix('refs/heads/')
        from .checkout import valid_branch
        valid_branch(branch)
        if branch.startswith('refs/'):
            raise GVError('--branch accepts branch names or refs/heads/NAME, not tags or PR refs', 'usage_error')
        result['branch'] = branch
    if 'user' in result and result['user'] != '@me':
        from .directory import login
        login(result['user'])
    if 'status' in result: result['status'] = result['status'].lower()
    if 'workflow' in result:
        value = result['workflow'].removeprefix('.gitverse/workflows/')
        if not filename(value) and ('/' in value or '\\' in value or value.isdecimal()):
            raise GVError('Use workflow filename or exact unique display name, not numeric ID/path', 'usage_error')
        result['workflow'] = value
    return result


def resolve_workflow(session, repo, selector):
    root = f'repos/{repo}/actions/workflows'
    if filename(selector): name = selector
    else:
        rows = catalog(session, root, 'workflows', workflow_row, lambda r: r['workflow'])
        matches = [r['workflow'] for r in rows if r['name'] == selector]
        if not matches: raise GVError('Workflow name not found', 'workflow_not_found')
        if len(matches) != 1: raise GVError('Workflow name is ambiguous; use filename', 'ambiguous_workflow', details={'workflows': matches})
        name = matches[0]
    path = root + '/' + quote(name, safe='')
    row = workflow_row(session.request('GET', path))
    if row['workflow'] != name: raise GVError('Workflow identity mismatch', 'unexpected_response')
    return name, path + '/runs'


def matches(row, selected):
    checks = []
    if 'branch' in selected:
        ref = text(row, 'ref')
        if not ref.startswith(('refs/heads/', 'refs/tags/', 'refs/pull/')):
            raise GVError('Unknown run ref namespace; cannot verify branch filter', 'unexpected_response', details={'run_id': row['id']})
        checks.append(ref == 'refs/heads/' + selected['branch'])
    if 'event' in selected: checks.append(text(row, 'event') == selected['event'])
    if 'status' in selected:
        status = text(row, 'status').lower()
        if selected['status'] == 'completed':
            state = bucket(status)
            if state == 'unknown': raise GVError('Unknown run status; cannot classify completed filter', 'unknown_run_status', details={'run_id': row['id']})
            approval = row.get('need_approval')
            if type(approval) is not bool: raise GVError('Unknown run approval requirement', 'unexpected_response', details={'run_id': row['id']})
            checks.append(state in ('pass', 'fail', 'cancel', 'skipping') and not approval)
        else: checks.append(status == selected['status'])
    if 'user' in selected:
        actor = row.get('actor')
        if not isinstance(actor, dict) or not isinstance(actor.get('login'), str) or not actor['login']:
            raise GVError('Unknown run actor; cannot verify user filter', 'unexpected_response', details={'run_id': row['id']})
        checks.append(actor['login'].casefold() == selected['user'].casefold())
    if 'commit' in selected:
        sha = text(row, 'commit_sha')
        if not re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', sha):
            raise GVError('Unknown run commit SHA', 'unexpected_response', details={'run_id': row['id']})
        checks.append(sha.lower() == selected['commit'])
    return all(checks)


def list_runs(client, repo, args):
    selected = filters(args)
    if client.dry_run:
        return {'dry_run': True, 'repository': repo, 'filters': selected, 'page': args.page, 'page_size': args.limit,
                'plan': 'Resolve @me/workflow; read full bounded visible catalog; validate all filter fields, AND filters, then page/limit/paginate. No implicit latest-run selection or network.'}
    session = Session(client, args)
    if selected.get('user') == '@me':
        from .directory import user_value
        selected['user'] = user_value(session.request('GET', 'user'))['login']
    path = f'repos/{repo}/actions/runs'
    workflow = None
    if 'workflow' in selected: workflow, path = resolve_workflow(session, repo, selected['workflow'])
    rows = catalog(session, path, 'workflow_runs', run_row, lambda r: r['id'])
    filtered = [r for r in rows if matches(r, selected)]
    result = page_result(filtered, 'workflow_runs', args, len(rows))
    result.update(repository=repo, total_count=len(filtered), filters=selected, workflow=workflow)
    result['coverage'].update(visibility='authenticated_user', scope='workflow' if workflow else 'repository',
                              local_filter_fields=[k for k in selected if k != 'workflow'],
                              workflow_scope='documented workflow-runs route' if workflow else None,
                              ordering='API order; newest run is not inferred',
                              ci_outcome_verified=False)
    return result
