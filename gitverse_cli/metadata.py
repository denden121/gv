"""PR metadata with preflight, separate auth surfaces and verified outcomes."""
import re
from urllib.parse import quote

from .client import Client, GVError


def names(values):
    result = []
    for value in values or []:
        for item in value.split(','):
            item = item.strip()
            if not item or any(ord(c) < 32 for c in item):
                raise GVError('Names must be non-empty, without control characters', 'usage_error')
            if item not in result:
                result.append(item)
    return result


def login_name(value):
    if value != '@me' and (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value) or value in ('.', '..')):
        raise GVError('Use a GitVerse login or @me; special GitHub accounts are unsupported', 'usage_error')
    return value


def login_values(values, client):
    result = []
    for value in values:
        login_name(value)
        if value == '@me':
            user = client.request('GET', 'user')
            value = user.get('login') if isinstance(user, dict) else None
            if not isinstance(value, str) or not value:
                raise GVError('Current user has no login', 'unexpected_response')
        if value.lower() not in {v.lower() for v in result}:
            result.append(value)
    return result


def users(values, client):
    result = []
    for login in login_values(values, client):
        user = client.request('GET', 'users/' + quote(login, safe=''))
        if not isinstance(user, dict) or type(user.get('id')) is not int or user['id'] <= 0 or not isinstance(user.get('login'), str) or user['login'].lower() != login.lower():
            raise GVError('User lookup did not confirm the requested identity', 'unexpected_response')
        result.append(user)
    return result


def create_assignees(values, client):
    """Resolve creation identities without trusting a login-only @me alias."""
    from .directory import user_value
    if len(values) > 100:
        raise GVError('At most 100 assignee names per creation', 'usage_error')
    current = user_value(client.request('GET', 'user')) if '@me' in values else None
    if current and current['login'] == '@me':
        raise GVError('Current user has no canonical login', 'unexpected_response')
    resolved, ids, logins = [], set(), set()
    for value in values:
        login = current['login'] if value == '@me' else value
        login_name(login)
        if login.lower() in logins:
            continue
        user = user_value(users([login], client)[0])
        if current and login.lower() == current['login'].lower() and user['id'] != current['id']:
            raise GVError('Self-assignee identity changed during lookup', 'unexpected_response')
        if user['id'] in ids:
            raise GVError('Assignee logins resolve to ambiguous IDs', 'unexpected_response')
        resolved.append(user)
        ids.add(user['id']); logins.add(user['login'].lower())
    return resolved


def assignee_identities(pull):
    from .directory import user_value
    rows = objects(pull, 'assignees', 'id')
    result, ids, logins = set(), set(), set()
    for row in rows:
        user = user_value(row)
        login_name(user['login'])
        login = user['login'].lower()
        if user['id'] in ids or login in logins:
            raise GVError('Duplicate assignee identity', 'unexpected_response')
        ids.add(user['id']); logins.add(login)
        result.add((user['id'], login))
    return result


def label_catalog(client, repo):
    result, seen = {}, set()
    for page in range(1, 101):
        rows = client.request('GET', f'repos/{repo}/labels', {'page': page, 'limit': 100})
        if not isinstance(rows, list):
            raise GVError('Unknown labels pagination response', 'unexpected_response')
        if len(rows) > 100:
            raise GVError('Label page exceeds requested size', 'pagination_limit')
        if not rows:
            return result
        for row in rows:
            if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0 or not isinstance(row.get('name'), str) or not row['name']:
                raise GVError('Invalid label identity', 'unexpected_response')
            if row['id'] in seen or row['name'] in result:
                raise GVError('Label pages repeat/shift or names are ambiguous', 'pagination_error')
            seen.add(row['id']); result[row['name']] = row
    raise GVError('Label pagination bound reached', 'pagination_limit')


def label_key(value):
    """Unicode simple case folding, without expanding e.g. sharp-s into ss."""
    return ''.join(c.casefold() if len(c.casefold()) == 1 else
                   c.lower() if len(c.lower()) == 1 else c for c in value)


def create_label_names(values):
    result = names(values)
    if len(result) > 100:
        raise GVError('At most 100 label names per creation', 'usage_error')
    for name in result:
        try:
            name.encode('utf-8')
        except UnicodeError:
            raise GVError('Label names must be UTF-8', 'usage_error') from None
        if len(name) > 40 or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise GVError('Label names must be at most 40 characters without controls', 'usage_error')
    return result


def create_labels(values, client, repo):
    if not values:
        return []
    catalog = {}
    for row in label_catalog(client, repo).values():
        if type(row.get('is_archived')) is not bool or type(row.get('exclusive')) is not bool:
            raise GVError('Label archive/exclusive state is incomplete', 'unexpected_response')
        key = label_key(row['name'])
        if key in catalog:
            raise GVError('Label names are ambiguous ignoring case', 'pagination_error')
        catalog[key] = row
    result, seen = [], set()
    for name in values:
        row = catalog.get(label_key(name))
        if row is None:
            raise GVError('Label not found in repository; labels are never created implicitly',
                          'unknown_label', details={'name': name})
        if row['is_archived']:
            raise GVError('Cannot assign an archived label', 'archived_label', details={'name': name})
        if row['id'] not in seen:
            result.append(row); seen.add(row['id'])
    validate_exclusive(result)
    return result


def label_identities(pull):
    result, ids, keys = set(), set(), set()
    for row in objects(pull, 'labels', 'id'):
        name = row.get('name')
        if not isinstance(name, str) or not name or row['id'] in ids or label_key(name) in keys:
            raise GVError('Invalid or duplicate label identity', 'unexpected_response')
        ids.add(row['id']); keys.add(label_key(name))
        result.add((row['id'], name))
    return result


def labels_to_add(values, client, repo):
    if not values:
        return []
    catalog = label_catalog(client, repo)
    result = []
    for name in values:
        if name not in catalog:
            raise GVError('Label not found in repository; labels are never created implicitly', 'unknown_label', details={'name': name})
        label = catalog[name]
        if label.get('is_archived') is True:
            raise GVError('Cannot assign an archived label', 'archived_label', details={'name': name})
        result.append(label)
    return result


def validate_exclusive(labels):
    scopes = set()
    for label in labels:
        if label.get('exclusive') is True:
            scope, sep, value = label['name'].partition('/')
            if not sep or not scope or not value or scope in scopes:
                raise GVError('Exclusive label scopes conflict; remove the existing scope value explicitly', 'exclusive_label_conflict')
            scopes.add(scope)


def requested_reviewers(values, client):
    result = []
    for value in values:
        if value.startswith('team:'):
            team = value.removeprefix('team:')
            if not team.isascii() or not team.isdecimal() or int(team) < 1:
                raise GVError('Team reviewer must be team:POSITIVE_ID', 'usage_error')
            result.append({'id': int(team), 'is_team': True})
        else:
            user = users([value], client)[0]
            result.append({'id': user['id'], 'is_team': False})
    return list({(r['id'], r['is_team']): r for r in result}.values())


def validate_reviewer(value):
    if value.startswith('team:'):
        team = value.removeprefix('team:')
        if not team.isascii() or not team.isdecimal() or int(team) < 1:
            raise GVError('Team reviewer must be team:POSITIVE_ID', 'usage_error')
    else:
        login_name(value)


def web_client():
    client = Client('web')
    if client.auth_source != 'cookie_file':
        raise GVError('Reviewer changes need a separate GITVERSE_COOKIE_FILE web session; PAT cannot authorize the internal API', 'web_session_required')
    return client


def pull_value(pull, number):
    if not isinstance(pull, dict) or type(pull.get('number')) is not int or pull['number'] != number:
        raise GVError('Unexpected PR identity', 'unexpected_response')
    return pull


def objects(pull, key, field):
    rows = pull.get(key)
    if not isinstance(rows, list):
        raise GVError('PR metadata is incomplete: ' + key, 'unexpected_response')
    for row in rows:
        value = row.get(field) if isinstance(row, dict) else None
        valid = type(value) is int and value > 0 if field == 'id' else isinstance(value, str) and bool(value)
        if not valid:
            raise GVError('PR metadata is incomplete: ' + key, 'unexpected_response')
    return rows


def reviewer_set(pull):
    result = set()
    for field, team in (('requested_reviewers', False), ('requested_teams', True)):
        for row in objects(pull, field, 'id'):
            key = (row['id'], team)
            if key in result:
                raise GVError('Duplicate requested reviewer identity', 'unexpected_response')
            result.add(key)
    return result


def same_metadata(actual, expected):
    for key, values in expected.items():
        if key == 'labels':
            got = {r['name'] for r in objects(actual, key, 'name')}
            if got != set(values):
                return False
        elif key == 'assignees':
            got = {r['login'].lower() for r in objects(actual, key, 'login')}
            if got != {v.lower() for v in values}:
                return False
        elif key == 'base':
            if not isinstance(actual.get('base'), dict) or actual['base'].get('ref') != values:
                return False
        elif actual.get(key) != values:
            return False
    return True


def enrich_error(error, stage, completed, number):
    details = dict(error.details or {})
    details.update(stage=stage, completed_steps=list(completed), pull_number=number, mutation_attempted=True,
                   recovery='Read the PR and its metadata before retrying; completed steps must not be repeated blindly.')
    return GVError(str(error), error.code, error.status, details)


def reviewer_change(web, repo, number, action, reviewer):
    return web.request('PATCH', f'repos/{repo}/pulls/{number}/reviewer', body={'action': action, 'reviewerId': reviewer['id'], 'isTeam': reviewer['is_team']})


def reviewer_preflight(web, repo, base, head, number=None):
    if not isinstance(base, str) or not base or not isinstance(head, str) or not head:
        raise GVError('Cannot identify reviewer candidate branches', 'unexpected_response')
    query = {'baseBranch': base, 'headBranch': head}
    if number is not None:
        query['pullIndex'] = number
    candidates = web.request('GET', f'repos/{repo}/pulls/reviewers', query)
    if not isinstance(candidates, dict) or not isinstance(candidates.get('users'), list) or not isinstance(candidates.get('teams'), list):
        raise GVError('Reviewer candidate response not confirmed; no mutation was sent', 'unexpected_response')
    return candidates


def create_with_metadata(args, client, repo, payload, web_transform=None):
    from .pr_create_reviewers import handles, resolve, preflight
    assignees, labels, reviewers = names(args.assignee), create_label_names(args.label), handles(args.reviewer)
    for value in assignees:
        login_name(value)
    if len(assignees) > 100:
        raise GVError('At most 100 assignee names per creation', 'usage_error')
    if client.dry_run:
        return {'base_request_template': client.request('POST', f'repos/{repo}/pulls', body=payload),
                'unresolved_metadata': {'assignees': assignees, 'labels': labels, 'reviewers': reviewers},
                'plan': 'Resolve all identities and existing label IDs before creation. Reviewers require separate cookies. Create once, verify assignees/labels via GET, attach reviewers individually through web API and verify requested IDs. Not atomic.'}
    web = web_client() if reviewers else None
    if web is not None and web_transform is not None:
        web = web_transform(web)
    assigned_rows = create_assignees(assignees, client)
    assigned = [u['login'] for u in assigned_rows]
    label_rows = create_labels(labels, client, repo)
    label_pins = {(r['id'], r['name']) for r in label_rows}
    review_rows = resolve(reviewers, client)
    if review_rows:
        preflight(web, client, repo, payload, review_rows)
    body = dict(payload)
    if assignees:
        body['assignees'] = assigned
    if labels:
        body['labels'] = [r['id'] for r in label_rows]
        fresh = create_labels(labels, client, repo)
        fields = ('id', 'name', 'exclusive', 'is_archived')
        if [[r[k] for k in fields] for r in fresh] != [[r[k] for k in fields] for r in label_rows]:
            raise GVError('Labels changed during creation preflight; no mutation was sent', 'concurrent_change')
    if reviewers and resolve(reviewers, client) != review_rows:
        raise GVError('Reviewer handle identities changed before creation', 'concurrent_change')
    completed, number, stage, reviewer_unknown, pending = [], None, 'create', False, None
    try:
        created = client.request('POST', f'repos/{repo}/pulls', body=body)
        if not isinstance(created, dict) or type(created.get('number')) is not int or created['number'] <= 0:
            raise GVError('Created PR identity not confirmed; inspect PR list', 'unexpected_response')
        number = created['number']; completed.append('create'); stage = 'verify_create_metadata'
        result = pull_value(client.request('GET', f'repos/{repo}/pulls/{number}'), number)
        if (assignees or labels or reviewers) and (type(created.get('id')) is not int or result.get('id') != created['id']):
            raise GVError('Created PR metadata identity was not confirmed', 'metadata_not_confirmed')
        if assignees and assignee_identities(result) != {(u['id'], u['login'].lower()) for u in assigned_rows}:
            raise GVError('Created assignee identities were not confirmed', 'metadata_not_confirmed')
        if labels and label_identities(result) != label_pins:
            raise GVError('Created label identities were not confirmed', 'metadata_not_confirmed')
        expected = {}
        if labels: expected['labels'] = [r['name'] for r in label_rows]
        if assignees: expected['assignees'] = assigned
        if not same_metadata(result, expected):
            raise GVError('Server did not confirm created PR metadata', 'metadata_not_confirmed')
        requested = reviewer_set(result) if review_rows else set()
        for row in review_rows:
            key = (row['id'], row['is_team'])
            if key in requested:
                continue
            stage = 'reviewer_preflight'
            preflight(web, client, repo, payload, [row], number)
            fresh = pull_value(client.request('GET', f'repos/{repo}/pulls/{number}'), number)
            if fresh.get('id') != created['id'] or reviewer_set(fresh) != requested:
                raise GVError('PR reviewer state changed before attachment', 'concurrent_change')
            stage = 'attach_reviewer'
            pending, reviewer_unknown = dict(row), True
            try:
                reviewer_change(web, repo, number, 'attach', row)
            except GVError as error:
                if error.code == 'http_error' and error.status in (400, 401, 403, 404, 422):
                    reviewer_unknown = False
                    error.details = {**(error.details or {}), 'reviewer_mutation_outcome': 'none'}
                raise
            stage = 'verify_reviewer'
            result = pull_value(client.request('GET', f'repos/{repo}/pulls/{number}'), number)
            requested.add(key)
            if result.get('id') != created['id'] or requested != reviewer_set(result):
                raise GVError('Reviewer attachment not confirmed by public PR response', 'metadata_not_confirmed')
            reviewer_unknown = False
            completed.append('attach:' + ('team:' if row['is_team'] else 'user:') + str(row['id']))
        stage = 'verify_final_metadata'
        if assignees and assignee_identities(result) != {(u['id'], u['login'].lower()) for u in assigned_rows}:
            raise GVError('Final assignee identities were not confirmed', 'metadata_not_confirmed')
        if labels and label_identities(result) != label_pins:
            raise GVError('Final label identities were not confirmed', 'metadata_not_confirmed')
        if not same_metadata(result, expected):
            raise GVError('Final created PR metadata not confirmed', 'metadata_not_confirmed')
        return {**result, 'metadata_completed_steps': completed}
    except GVError as error:
        if pending is not None:
            error.details = {**(error.details or {}), 'reviewer': pending,
                             'reviewer_mutation_outcome': (error.details or {}).get('reviewer_mutation_outcome', 'unknown' if reviewer_unknown else 'confirmed'),
                             **({'mutation_outcome': 'unknown'} if reviewer_unknown else {})}
        raise enrich_error(error, stage, completed, number) from error


def edit_with_metadata(args, client, repo, number, plain):
    changes = {kind: {'add': names(getattr(args, 'add_' + kind)), 'remove': names(getattr(args, 'remove_' + kind))} for kind in ('assignee', 'label', 'reviewer')}
    for kind, change in changes.items():
        for value in change['add'] + change['remove']:
            if kind == 'assignee': login_name(value)
            elif kind == 'reviewer': validate_reviewer(value)
        if {v.lower() if kind != 'label' else v for v in change['add']} & {v.lower() if kind != 'label' else v for v in change['remove']}:
            raise GVError('Cannot add and remove the same ' + kind, 'usage_error')
    path = f'repos/{repo}/pulls/{number}'
    if client.dry_run:
        return {'read_request': client.request('GET', path), 'changes': changes, 'plain_changes': plain,
                'plan': 'Resolve all names, read-modify-write assignee/label sets through public PATCH issues, reviewers through cookie-only web PATCH reviewer. Precheck related fields, verify each step, preserve completed_steps on error. Not atomic.'}
    review_changes = changes['reviewer']['add'] or changes['reviewer']['remove']
    web = web_client() if review_changes else None
    before = pull_value(client.request('GET', path), number)
    issue, compare, expected = {}, {}, dict(plain)
    for kind, field in (('assignee', 'assignees'), ('label', 'labels')):
        change = changes[kind]
        if not (change['add'] or change['remove']):
            continue
        column = 'login' if kind == 'assignee' else 'name'
        current = objects(before, field, column)
        current_values = [r[column] for r in current]
        compare[field] = current_values
        if kind == 'assignee':
            added = [u['login'] for u in users(change['add'], client)]
            removed = login_values(change['remove'], client)
            if {v.lower() for v in added} & {v.lower() for v in removed}:
                raise GVError('Resolved assignee is both added and removed', 'usage_error')
            target = {v.lower(): v for v in current_values if v.lower() not in {r.lower() for r in removed}}
            target.update({v.lower(): v for v in added})
            desired = list(target.values())
        else:
            added = labels_to_add(change['add'], client, repo)
            target = {r['name']: r for r in current if r['name'] not in change['remove']}
            target.update({r['name']: r for r in added})
            validate_exclusive(target.values())
            desired = list(target)
        expected[field] = desired
        norm = (lambda values: {v.lower() for v in values}) if kind == 'assignee' else set
        if norm(desired) != norm(current_values):
            issue[field] = desired
    add_reviews = requested_reviewers(changes['reviewer']['add'], client)
    remove_reviews = requested_reviewers(changes['reviewer']['remove'], client)
    add_keys = {(r['id'], r['is_team']) for r in add_reviews}
    remove_keys = {(r['id'], r['is_team']) for r in remove_reviews}
    if add_keys & remove_keys:
        raise GVError('Resolved reviewer is both added and removed', 'usage_error')
    old_reviewers = reviewer_set(before) if review_changes else set()
    desired_reviewers = (old_reviewers - remove_keys) | add_keys
    author = before.get('user') or {}
    if add_reviews and (not isinstance(author, dict) or type(author.get('id')) is not int):
        raise GVError('PR author identity is incomplete', 'unexpected_response')
    if any(not r['is_team'] and r['id'] == author.get('id') for r in add_reviews):
        raise GVError('The PR author cannot be requested as reviewer', 'usage_error')
    operations = [('detach', r) for r in remove_reviews if (r['id'], r['is_team']) in old_reviewers] + [('attach', r) for r in add_reviews if (r['id'], r['is_team']) not in old_reviewers]
    if operations:
        base, head = before.get('base'), before.get('head')
        if not isinstance(base, dict) or not isinstance(head, dict):
            raise GVError('PR branches are incomplete', 'unexpected_response')
        reviewer_preflight(web, repo, plain.get('base', base.get('ref')), head.get('ref'), number)
    # Compare only fields we will overwrite. No documented atomic conditional
    # update exists, so this narrows but cannot eliminate the race window.
    for key in plain:
        if key == 'base':
            if not isinstance(before.get('base'), dict) or not isinstance(before['base'].get('ref'), str):
                raise GVError('PR base branch is incomplete', 'unexpected_response')
            compare[key] = before['base']['ref']
        else:
            compare[key] = before.get(key)
    latest = pull_value(client.request('GET', path), number)
    if not same_metadata(latest, compare) or (review_changes and reviewer_set(latest) != old_reviewers):
        raise GVError('PR metadata changed during preflight; no mutation was sent', 'pr_changed')
    completed, stage = [], 'update_pull'
    try:
        if plain:
            client.request('PATCH', path, body=plain)
            stage = 'verify_pull'
            latest = pull_value(client.request('GET', path), number)
            if not same_metadata(latest, plain):
                raise GVError('PR title/body/base update not confirmed', 'metadata_not_confirmed')
            completed.append('pull_metadata')
        if issue:
            stage = 'update_issue'
            if not same_metadata(latest, {key: compare[key] for key in issue}):
                raise GVError('Assignee/label state changed before overwrite', 'pr_changed')
            client.request('PATCH', f'repos/{repo}/issues/{number}', body=issue)
            stage = 'verify_issue'
            latest = pull_value(client.request('GET', path), number)
            if not same_metadata(latest, expected):
                raise GVError('Assignee/label update not confirmed', 'metadata_not_confirmed')
            completed.append('issue_metadata')
        present = set(old_reviewers)
        for action, row in operations:
            stage = action + '_reviewer'
            reviewer_change(web, repo, number, action, row)
            stage = 'verify_reviewer'
            latest = pull_value(client.request('GET', path), number)
            key = (row['id'], row['is_team'])
            if action == 'attach': present.add(key)
            else: present.discard(key)
            if reviewer_set(latest) != present:
                raise GVError('Reviewer update not confirmed', 'metadata_not_confirmed')
            completed.append(action + ':' + ('team:' if row['is_team'] else 'user:') + str(row['id']))
        if not same_metadata(latest, expected) or (review_changes and reviewer_set(latest) != desired_reviewers):
            raise GVError('Final metadata state not confirmed', 'metadata_not_confirmed')
        return {**latest, 'metadata_changed': bool(completed), 'metadata_completed_steps': completed}
    except GVError as error:
        raise enrich_error(error, stage, completed, number) from error
