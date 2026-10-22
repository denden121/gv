"""Verified single-request issue/label mutations and bounded catalog reads."""
import os
import re
import time
from urllib.parse import urlsplit

from .client import GVError
from .metadata import login_values, names, objects, same_metadata, users, validate_exclusive


class Session:
    def __init__(self, client, args):
        self.client, self.args = client, args
        self.deadline = time.monotonic() + args.timeout

    def request(self, method, path, query=None, body=None):
        return self.client.request(method, path, query, body=body, deadline=self.deadline,
                                   require_complete=True, max_response_bytes=33554432)

    def catalog(self, path, page_key='per_page', query=None, size=100):
        rows, seen = [], set()
        for page in range(1, self.args.max_pages + 1):
            batch = self.request('GET', path, {**(query or {}), 'page': page, page_key: size})
            if not isinstance(batch, list): raise GVError('Unknown issue/label catalog', 'unexpected_response')
            if not batch: return rows
            for row in batch:
                if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0:
                    raise GVError('Missing issue/label identity', 'unexpected_response')
                if row['id'] in seen: raise GVError('Repeated or shifting catalog pages', 'pagination_error')
                seen.add(row['id']); rows.append(row)
                if len(rows) > 100000: raise GVError('Catalog entry bound reached', 'catalog_limit')
        raise GVError('Catalog page bound reached; no partial result returned', 'pagination_limit')


def issue_target(args):
    if not hasattr(args, 'selector'): return
    value = args.selector
    if re.fullmatch(r'[0-9]+', value) and int(value) > 0:
        args.number = int(value); return
    url = urlsplit(value)
    match = re.fullmatch(r'/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)/?', url.path)
    if url.scheme != 'https' or url.netloc != os.getenv('GV_GIT_HOST', 'gitverse.ru') or url.query or url.fragment or not match:
        raise GVError('Use an issue number or canonical HTTPS GitVerse /OWNER/REPO/issues/N URL', 'usage_error')
    from .cli import parse_repo
    repo = parse_repo(match[1] + '/' + match[2])
    explicit = getattr(args, 'repo', None) or os.getenv('GV_REPO')
    if explicit and parse_repo(explicit) != repo: raise GVError('Issue URL conflicts with repository', 'invalid_repo')
    args.repo, args.number = repo, int(match[3])


def title(value):
    if not isinstance(value, str) or not value.strip() or any(c in value for c in ('\0', '\r', '\n')):
        raise GVError('Title must be a non-blank single line', 'usage_error')
    return value


def issue_value(row, number=None, allow_pull=False):
    if not isinstance(row, dict) or type(row.get('number')) is not int or row['number'] <= 0 or (number is not None and row['number'] != number) or row.get('state') not in ('open', 'closed') or not isinstance(row.get('title'), str):
        raise GVError('Invalid issue identity/state/title', 'unexpected_response')
    pull = row.get('pull_request')
    if pull is not None and not isinstance(pull, dict): raise GVError('Unknown pull request discriminator', 'unexpected_response')
    if pull is not None and not allow_pull: raise GVError('This number belongs to a PR; use gv pr', 'issue_is_pull_request')
    return row


def label_value(row):
    if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0 or not isinstance(row.get('name'), str) or not row['name'] or not isinstance(row.get('color'), str) or not re.fullmatch(r'[0-9a-f]{6}', row['color']) or type(row.get('exclusive')) is not bool or type(row.get('is_archived')) is not bool:
        raise GVError('Invalid label identity/metadata', 'unexpected_response')
    return row


def labels(session, root):
    rows = session.catalog(root + '/labels', 'limit')
    result = {}
    for row in rows:
        label_value(row)
        if row['name'] in result: raise GVError('Duplicate label names', 'pagination_error')
        result[row['name']] = row
    return result


def resolve_labels(wanted, catalog):
    result = []
    for name in wanted:
        if name not in catalog: raise GVError('Label not found; create it explicitly', 'unknown_label', details={'name': name})
        row = catalog[name]
        if row['is_archived']: raise GVError('Cannot assign an archived label', 'archived_label')
        result.append(row)
    validate_exclusive(result)
    return result


def page_result(rows, key, args, scanned):
    start = (args.page - 1) * args.limit
    selected = rows[start:] if args.paginate else rows[start:start + args.limit]
    return {key: selected, 'total_matches': len(rows), 'page': args.page, 'page_size': args.limit,
            'has_more': start + len(selected) < len(rows),
            'coverage': {'catalog_complete': True, 'entries_scanned': scanned, 'atomic_snapshot': False}}


def write(session, method, path, payload, verify, identity):
    stage = 'write'
    try:
        result = session.request(method, path, body=payload)
        stage = 'verify'
        return verify(result)
    except GVError as error:
        details = dict(error.details or {})
        details.update(identity, stage=stage, mutation_attempted=True,
                       recovery='Read the resource/catalog before retrying; a failed or unconfirmed write may have applied. No automatic mutation retry.')
        raise GVError(str(error), error.code, error.status, details) from error


def issue_command(client, repo, args):
    from .cli import body_text
    root = f'repos/{repo}'
    action = args.action
    payload = {}
    if action in ('create', 'edit'):
        if args.title is not None: payload['title'] = title(args.title)
        if args.body is not None or args.body_file is not None: payload['body'] = body_text(args)
    if action == 'comment': payload['body'] = body_text(args, required=True)
    if action == 'create':
        payload.setdefault('body', '')
        add_labels, add_users = names(args.label), names(args.assignee)
        remove_labels, remove_users = [], []
    elif action == 'edit':
        add_labels, remove_labels = names(args.add_label), names(args.remove_label)
        add_users, remove_users = names(args.add_assignee), names(args.remove_assignee)
        if set(add_labels) & set(remove_labels) or {v.lower() for v in add_users} & {v.lower() for v in remove_users}:
            raise GVError('The same name cannot be added and removed', 'usage_error')
        if not payload and not any((add_labels, remove_labels, add_users, remove_users)): raise GVError('Specify issue fields to edit', 'usage_error')
    else: add_labels = add_users = remove_labels = remove_users = []
    if client.dry_run:
        return {'repository': repo, 'action': action, 'issue_number': getattr(args, 'number', None), 'payload': payload,
                'unresolved_labels': {'add': add_labels, 'remove': remove_labels},
                'unresolved_assignees': {'add': add_users, 'remove': remove_users},
                'plan': 'Public issue API only. Resolve identities/labels, reject PRs, read before write and verify afterward. No network/writes.'}
    session = Session(client, args)
    if action == 'list':
        rows = session.catalog(root + '/issues', query={'state': 'all', 'type': 'issues+pulls'}, size=min(args.limit, 100))
        authors = login_values([args.author], session) if args.author else []
        assignees = login_values([args.assignee], session) if args.assignee else []
        label_names = set(names(args.label))
        selected, numbers = [], set()
        for row in rows:
            issue_value(row, allow_pull=True)
            if row['number'] in numbers: raise GVError('Issue catalog repeats a number', 'pagination_error')
            numbers.add(row['number'])
            if row.get('pull_request') is not None: continue
            if args.state != 'all' and row['state'] != args.state: continue
            if authors:
                user = row.get('user')
                if not isinstance(user, dict) or not isinstance(user.get('login'), str): raise GVError('Missing issue author', 'unexpected_response')
                if user['login'].lower() != authors[0].lower(): continue
            if assignees and assignees[0].lower() not in {r['login'].lower() for r in objects(row, 'assignees', 'login')}: continue
            if label_names and not label_names <= {r['name'] for r in objects(row, 'labels', 'name')}: continue
            if args.search_text is not None:
                if not isinstance(row.get('body'), str): raise GVError('Missing issue body for filtering', 'unexpected_response')
                if args.search_text.casefold() not in (row['title'] + '\n' + row['body']).casefold(): continue
            selected.append(row)
        return page_result(selected, 'issues', args, len(rows))
    number = getattr(args, 'number', None)
    path = root + '/issues' + (f'/{number}' if number else '')
    before = issue_value(session.request('GET', path), number) if action != 'create' else None
    if action == 'view': return before
    if action == 'comments':
        result = session.request('GET', path + '/comments')
        if not isinstance(result, list): raise GVError('Unknown issue comments response', 'unexpected_response')
        return {'comments': result, 'coverage': {'pagination': 'No pagination parameters documented; one endpoint response'}}
    if action in ('close', 'reopen'): payload['state'] = 'closed' if action == 'close' else 'open'
    if add_labels or remove_labels:
        catalog = labels(session, root)
        resolve_labels(add_labels, catalog)
        previous = {r['name'] for r in objects(before, 'labels', 'name')} if before else set()
        desired = (previous - set(remove_labels)) | set(add_labels)
        # Existing archived labels may stay, but new assignments must be active.
        chosen = [catalog[name] for name in desired if name in catalog]
        if len(chosen) != len(desired): raise GVError('Existing issue label missing from catalog', 'unexpected_response')
        validate_exclusive(chosen)
        payload['labels'] = sorted(desired)
    if add_users or remove_users:
        additions = [row['login'] for row in users(add_users, session)]
        removals = login_values(remove_users, session)
        if {v.lower() for v in additions} & {v.lower() for v in removals}: raise GVError('Resolved user is both added and removed', 'usage_error')
        previous = [r['login'] for r in objects(before, 'assignees', 'login')] if before else []
        desired = {v.lower(): v for v in previous if v.lower() not in {r.lower() for r in removals}}
        desired.update({v.lower(): v for v in additions})
        payload['assignees'] = sorted(desired.values())
    if before is not None:
        if action != 'comment' and same_metadata(before, payload): return {'issue': before, 'changed': False}
        latest = issue_value(session.request('GET', path), number)
        if latest['state'] != before['state'] or not same_metadata(latest, {k: before[k] for k in payload if k not in ('labels', 'assignees', 'body')}) or ('body' in payload and latest.get('body') != before.get('body')) or any(key in payload and {r[field] for r in objects(latest, key, field)} != {r[field] for r in objects(before, key, field)} for key, field in (('labels', 'name'), ('assignees', 'login'))):
            raise GVError('Issue fields changed before write; reread before editing', 'concurrent_change')
    if action == 'comment':
        comment_identity = {'repository': repo, 'issue_number': number}
        def verify_comment(result):
            if not isinstance(result, dict) or type(result.get('id')) is not int or result['id'] <= 0: raise GVError('Missing created comment ID', 'unexpected_response')
            comment_identity['comment_id'] = result['id']
            confirmed = session.request('GET', root + f"/issues/comments/{result['id']}")
            if not isinstance(confirmed, dict) or confirmed.get('id') != result['id'] or confirmed.get('body') != payload['body']: raise GVError('Comment write not confirmed', 'verification_failed')
            return {'comment': confirmed, 'issue_number': number, 'changed': True}
        return write(session, 'POST', path + '/comments', payload, verify_comment, comment_identity)
    def verify_issue(result):
        nonlocal number
        if action == 'create': number = issue_value(result)['number']
        confirmed = issue_value(session.request('GET', root + f'/issues/{number}'), number)
        if not same_metadata(confirmed, payload): raise GVError('Issue write not confirmed', 'verification_failed', details={'issue_number': number})
        return {'issue': confirmed, 'changed': True}
    identity = {'repository': repo, 'issue_number': number}
    # Preserve the created number even if the following GET fails.
    def verify(result):
        if action == 'create' and isinstance(result, dict) and type(result.get('number')) is int: identity['issue_number'] = result['number']
        return verify_issue(result)
    return write(session, 'POST' if action == 'create' else 'PATCH', path, payload, verify, identity)


def label_command(client, repo, args):
    root = f'repos/{repo}'
    action, payload = args.action, {}
    if action != 'list':
        if not args.name or len(args.name) > 40 or any(ord(c) < 32 or ord(c) == 127 for c in args.name): raise GVError('Label name must contain 1..40 characters without controls', 'usage_error')
    if action in ('create', 'edit'):
        if action == 'create': payload['name'] = args.name
        elif args.new_name is not None:
            if not args.new_name or len(args.new_name) > 40 or any(ord(c) < 32 or ord(c) == 127 for c in args.new_name): raise GVError('Invalid new label name', 'usage_error')
            payload['name'] = args.new_name
        if args.color is not None:
            color = args.color.removeprefix('#')
            if not re.fullmatch(r'[A-Fa-f0-9]{3}|[A-Fa-f0-9]{6}', color): raise GVError('Color must be 3/6 hexadecimal digits', 'usage_error')
            payload['color'] = ''.join(c * 2 for c in color).lower() if len(color) == 3 else color.lower()
        if args.description is not None:
            if len(args.description) > 100: raise GVError('Label description exceeds 100 characters', 'usage_error')
            payload['description'] = args.description
        for key in ('exclusive', 'is_archived'):
            if getattr(args, key) is not None: payload[key] = getattr(args, key)
        if not payload: raise GVError('Specify label fields to edit', 'usage_error')
        if payload.get('exclusive') is True:
            validate_exclusive([{'name': payload.get('name', args.name), 'exclusive': True}])
    if client.dry_run: return {'repository': repo, 'action': action, 'name': getattr(args, 'name', None), 'payload': payload, 'plan': 'Fully read label catalog, resolve exact name to ID, precheck, mutate once, verify by ID. No network/writes.'}
    session = Session(client, args)
    catalog = labels(session, root)
    if action == 'list': return page_result(list(catalog.values()), 'labels', args, len(catalog))
    before = catalog.get(args.name)
    if action == 'create':
        if before: raise GVError('Label already exists; use label edit', 'label_exists')
    elif not before: raise GVError('Label not found', 'unknown_label')
    if action == 'view': return before
    if action == 'edit':
        if payload.get('name') in catalog and catalog[payload['name']]['id'] != before['id']: raise GVError('New label name already exists', 'label_exists')
        validate_exclusive([{**before, **payload}])
        if same_metadata(before, payload): return {'label': before, 'changed': False}
        latest = label_value(session.request('GET', root + f"/labels/{before['id']}"))
        if latest['id'] != before['id'] or any(latest.get(k) != before.get(k) for k in ('name', 'color', 'description', 'exclusive', 'is_archived')): raise GVError('Label changed before edit', 'concurrent_change')
    identity = {'repository': repo, 'label_id': before['id'] if before else None}
    def verify(result):
        row = label_value(result)
        identity['label_id'] = row['id']
        if before and row['id'] != before['id']: raise GVError('Updated label identity changed', 'verification_failed')
        confirmed = label_value(session.request('GET', root + f"/labels/{row['id']}"))
        if confirmed['id'] != row['id'] or not same_metadata(confirmed, payload): raise GVError('Label write not confirmed', 'verification_failed')
        return {'label': confirmed, 'changed': True}
    return write(session, 'POST' if action == 'create' else 'PATCH', root + '/labels' + (f"/{before['id']}" if before else ''), payload, verify, identity)
