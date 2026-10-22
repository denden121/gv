"""Public workflow metadata and single-shot dispatch with validated string inputs."""
import json
import math
from pathlib import Path
import sys
import time
from urllib.parse import quote

from .client import GVError


def clean(value, label):
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise GVError(label + ' must be non-empty, without control characters', 'usage_error')
    return value


def filename(value):
    return '/' not in value and '\\' not in value and value not in ('.', '..') and value.endswith(('.yml', '.yaml'))


def workflow_row(row):
    if not isinstance(row, dict) or not isinstance(row.get('workflow'), str) or not filename(row['workflow']) or not isinstance(row.get('name'), str) or not isinstance(row.get('state'), str) or not row['state']:
        raise GVError('Incomplete workflow metadata', 'unexpected_response')
    return row


def catalog(client, repo, args, deadline):
    rows, seen, expected = [], set(), None
    for page in range(1, args.max_pages + 1):
        data = client.request('GET', f'repos/{repo}/actions/workflows', {'page': page, 'per_page': 100}, deadline=deadline)
        if not isinstance(data, dict) or not isinstance(data.get('workflows'), list):
            raise GVError('Unknown workflow list response', 'unexpected_response')
        total = data.get('total_count')
        if total is not None:
            if type(total) is not int or total < 0 or (expected is not None and expected != total):
                raise GVError('Invalid or changing workflow count', 'pagination_error')
            expected = total
        batch = data['workflows']
        for row in batch:
            workflow_row(row)
            if row['workflow'] in seen:
                raise GVError('Repeated or shifting workflow pages', 'pagination_error')
            seen.add(row['workflow']); rows.append(row)
        if expected is not None and len(rows) > expected:
            raise GVError('Workflow count contradicts pages', 'pagination_error')
        if expected is not None and len(rows) == expected: return rows
        if not batch:
            if expected is not None: raise GVError('Workflow pages ended before total_count', 'pagination_error')
            return rows
    raise GVError('Workflow pagination bound reached', 'pagination_limit')


def resolve(client, repo, args, deadline):
    value = clean(args.workflow, 'Workflow selector')
    prefix = '.gitverse/workflows/'
    if value.startswith(prefix): value = value[len(prefix):]
    if filename(value): return value
    if '/' in value or '\\' in value or value.isdecimal():
        raise GVError('Use a workflow filename or exact display name, not a path or numeric ID', 'usage_error')
    matches = [r['workflow'] for r in catalog(client, repo, args, deadline) if r['name'] == value]
    if not matches: raise GVError('Workflow name not found', 'workflow_not_found')
    if len(matches) != 1:
        raise GVError('Workflow name is ambiguous; use a filename', 'ambiguous_workflow', details={'workflows': matches})
    return matches[0]


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise GVError('Duplicate workflow input key', 'usage_error')
        result[key] = value
    return result


def input_values(args):
    if args.input:
        text = sys.stdin.read() if args.input == '-' else Path(args.input).read_text(encoding='utf-8')
        values = json.loads(text, object_pairs_hook=unique_object)
    else:
        pairs = []
        for field in args.input_fields or []:
            key, sep, value = field.partition('=')
            if not sep: raise GVError('Inputs must be KEY=VALUE', 'usage_error')
            pairs.append((key, value))
        values = unique_object(pairs)
    if not isinstance(values, dict): raise GVError('Workflow inputs must be a JSON object of strings', 'usage_error')
    for key, value in values.items():
        clean(key, 'Input name')
        if not isinstance(value, str): raise GVError('Workflow input values must be strings', 'usage_error')
    return values


def validate_inputs(data, values):
    if not isinstance(data, dict) or not isinstance(data.get('inputs'), list):
        raise GVError('Unknown dispatch input metadata', 'unexpected_response')
    known = {}
    for item in data['inputs']:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not item['name'] or item['name'] in known or type(item.get('required')) is not bool or not isinstance(item.get('type'), str):
            raise GVError('Incomplete dispatch input metadata', 'unexpected_response')
        known[item['name']] = item
    if set(values) - set(known):
        raise GVError('Unknown workflow input names', 'usage_error', details={'names': sorted(set(values) - set(known))})
    for key, item in known.items():
        if item['required'] and not values.get(key) and not item.get('default'):
            raise GVError('Required workflow input is missing', 'usage_error', details={'name': key})
        if key not in values: continue
        value, kind = values[key], item['type']
        if kind == 'boolean' and value not in ('true', 'false'):
            raise GVError('Boolean workflow input must be true or false', 'usage_error', details={'name': key})
        if kind == 'choice':
            options = item.get('options')
            if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
                raise GVError('Invalid choice input metadata', 'unexpected_response')
            if value not in options: raise GVError('Workflow choice is not one of the allowed options', 'usage_error', details={'name': key})
        if kind == 'number':
            try: valid = bool(value.strip()) and math.isfinite(float(value))
            except ValueError: valid = False
            if not valid: raise GVError('Workflow number input must be finite', 'usage_error', details={'name': key})
        if kind not in ('string', 'boolean', 'choice', 'number', 'environment'):
            raise GVError('Unsupported workflow input type', 'unexpected_response', details={'name': key, 'type': kind})


def workflow_command(client, repo, args):
    if args.action == 'list':
        if client.dry_run:
            return {'request': client.request('GET', f'repos/{repo}/actions/workflows', {'page': 1, 'per_page': 100}),
                    'plan': 'Read complete bounded catalog, filter active unless --all, then apply page/limit/paginate.', 'max_pages': args.max_pages}
        rows = catalog(client, repo, args, time.monotonic() + args.timeout)
        if not args.all: rows = [r for r in rows if r['state'] == 'active']
        start = (args.page - 1) * args.limit
        return rows[start:] if args.paginate else rows[start:start + args.limit]
    clean(args.workflow, 'Workflow selector')
    if args.ref is not None: clean(args.ref, 'Branch')
    if args.tag is not None: clean(args.tag, 'Tag')
    if args.action == 'view' and not args.inputs and (args.ref or args.tag):
        raise GVError('workflow view --ref/--tag requires --inputs; source YAML viewing is not implemented', 'usage_error')
    values = input_values(args) if args.action == 'run' else None
    if client.dry_run:
        return {'workflow_selector': args.workflow, 'target': {'branch': args.ref} if args.ref else ({'tag': args.tag} if args.tag else {'repository_default_branch': True}),
                'inputs': values, 'plan': 'Resolve filename or exact unique name, read metadata and dispatch inputs for target. For run: validate string inputs, POST once, never infer run ID from latest runs.'}
    deadline = time.monotonic() + args.timeout
    name = resolve(client, repo, args, deadline)
    path = f'repos/{repo}/actions/workflows/{quote(name, safe="")}'
    row = workflow_row(client.request('GET', path, deadline=deadline))
    if row['workflow'] != name: raise GVError('Workflow identity mismatch', 'unexpected_response')
    if args.action == 'view' and not args.inputs: return row
    if args.tag: target = {'tag': args.tag}
    else:
        branch = args.ref
        if branch is None:
            repository = client.request('GET', f'repos/{repo}', deadline=deadline)
            branch = repository.get('default_branch') if isinstance(repository, dict) else None
            if not isinstance(branch, str) or not branch: raise GVError('Unknown default branch', 'unexpected_response')
        target = {'branch': branch}
    parameters = client.request('GET', path + '/dispatches', target, deadline=deadline)
    if args.action == 'view': return {'workflow': row, 'target': target, 'dispatch': parameters}
    if row['state'] != 'active': raise GVError('Cannot dispatch inactive workflow', 'workflow_inactive')
    validate_inputs(parameters, values)
    try:
        response = client.request('POST', path + '/dispatches', target, body=values, deadline=deadline)
        if response is not None: raise GVError('Unexpected dispatch response; inspect runs before any retry', 'unexpected_response')
    except GVError as error:
        raise GVError(str(error), error.code, error.status,
                      {'stage': 'dispatch', 'mutation_attempted': True, 'workflow': name, 'target': target,
                       'recovery': 'Inspect workflow runs before retrying; the dispatch may already have been accepted. No automatic write retry.'}) from error
    return {'accepted': True, 'workflow': name, 'target': target, 'run_id': None,
            'note': 'Dispatch returned no run ID. Find the new workflow_dispatch run explicitly; acceptance does not mean CI success.'}
