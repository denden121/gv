"""Read-only organization/team discovery using the bundled public contract."""
import re
from urllib.parse import quote

from .client import GVError
from .issues import Session, page_result


def login(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', value):
        raise GVError('Use an organization/user login, not a URL or path', 'usage_error')
    return value


def user_value(row, expected=None, organization=False):
    if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0 or not isinstance(row.get('login'), str) or not row['login']:
        raise GVError('Invalid directory user identity', 'unexpected_response')
    if expected is not None and row['login'].casefold() != expected.casefold():
        raise GVError('Directory profile identity mismatch', 'unexpected_response')
    if organization and row.get('type') != 'Organization':
        raise GVError('This account is not an organization', 'not_organization')
    return row


def team_value(row):
    if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0 or not isinstance(row.get('slug'), str) or not row['slug'] or not isinstance(row.get('name'), str):
        raise GVError('Invalid team identity', 'unexpected_response')
    return row


def repo_value(row):
    if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0 or not isinstance(row.get('full_name'), str) or len(row['full_name'].split('/')) != 2 or not all(row['full_name'].split('/')):
        raise GVError('Invalid directory repository identity', 'unexpected_response')
    return row


def catalog(session, path, validate, query=None):
    rows, seen, names = [], set(), set()
    for page in range(1, session.args.max_pages + 1):
        batch = session.request('GET', path, {**(query or {}), 'per_page': 100, 'page': page})
        if not isinstance(batch, list):
            raise GVError('Unknown directory catalog shape', 'unexpected_response')
        if not batch: return rows
        for item in batch:
            validate(item)
            name = item.get('slug', item.get('login', item.get('full_name', ''))).casefold()
            if item['id'] in seen or name in names:
                raise GVError('Repeated or shifting directory pages', 'pagination_error')
            seen.add(item['id']); names.add(name); rows.append(item)
            if len(rows) > 100000:
                raise GVError('Directory entry bound reached', 'catalog_limit')
    raise GVError('Directory page bound reached; no partial result returned', 'pagination_limit')


def directory_command(client, args):
    if getattr(args, 'repo', None):
        raise GVError('Organization/team commands do not accept -R; pass an organization explicitly', 'usage_error')
    slug = None
    if args.group == 'org':
        org = login(args.organization)
    elif args.action == 'list':
        org = login(args.org)
    else:
        parts = args.team.split('/')
        if len(parts) != 2:
            raise GVError('Use ORG/SLUG for a team; IDs are output metadata', 'usage_error')
        org, slug = login(parts[0]), parts[1]
        if not slug or len(slug) > 200 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in '/?#%\\' for c in slug) or slug in ('.', '..'):
            raise GVError('Invalid team slug', 'usage_error')
    username = None
    if args.group == 'org' and args.action == 'member':
        username = args.username if args.username == '@me' else login(args.username)
    root = 'orgs/' + quote(org, safe='')
    if client.dry_run:
        return {'dry_run': True, 'group': args.group, 'action': args.action, 'organization': org,
                'team_slug': slug, 'username': username, 'writes': False,
                'plan': 'Verify organization profile; read bounded visible catalogs or documented membership status. Identities unresolved.'}
    session = Session(client, args)
    profile = user_value(session.request('GET', 'users/' + quote(org, safe='')), org, True)
    if args.group == 'org' and args.action == 'view':
        return {'organization': profile}
    if username is not None:
        user = user_value(session.request('GET', 'user' if username == '@me' else 'users/' + quote(username, safe='')), None if username == '@me' else username)
        member_path = root + '/members/' + quote(user['login'], safe='')
        try:
            result = client.request('GET', member_path, deadline=session.deadline, require_complete=True,
                                    max_response_bytes=33554432, expected_status=204)
            if result is not None: raise GVError('Membership response must be empty', 'unexpected_response')
            member, evidence = True, 'documented_http_204'
        except GVError as error:
            if error.code != 'http_error' or error.status != 302: raise
            # NoRedirect prevents following this documented negative response with PAT.
            member, evidence = False, 'documented_http_302'
        return {'organization': profile['login'], 'username': user['login'], 'is_member': member, 'evidence': evidence}
    if args.group == 'org':
        rows = catalog(session, root + '/repos', repo_value)
        key = 'repositories'
    else:
        teams = catalog(session, root + '/teams', team_value)
        if args.action == 'list':
            rows, key = teams, 'teams'
        else:
            matches = [t for t in teams if t['slug'] == slug]
            if not matches:
                raise GVError('Team is not present in the visible organization catalog', 'team_not_found',
                              details={'organization': profile['login'], 'slug': slug, 'visibility': 'authenticated_user'})
            team = matches[0]
            if args.action == 'view':
                return {'organization': profile['login'], 'team': team,
                        'coverage': {'visible_teams_only': True, 'catalog_complete': True, 'atomic_snapshot': False}}
            path = root + '/teams/' + quote(slug, safe='') + '/' + args.action
            rows = catalog(session, path, user_value if args.action == 'members' else repo_value,
                           {'role': args.role} if args.action == 'members' and args.role else None)
            key = 'members' if args.action == 'members' else 'repositories'
    result = page_result(rows, key, args, len(rows))
    result['organization'] = profile['login']
    result['coverage']['visibility'] = 'authenticated_user; inaccessible records are unknown'
    if slug is not None:
        result['team'] = team
        if args.action == 'members':
            result['coverage']['includes_child_team_members'] = True
            result['role'] = args.role or 'all'
    return result
