"""Bounded reviewer handle resolution for creation; no credential sharing."""
import re
from urllib.parse import quote

from .client import GVError


def handles(values):
    if (any(not isinstance(v, str) or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in v)
            for v in values or [])
            or sum(len(v.encode('utf-8', errors='replace')) for v in values or []) > 1048576):
        raise GVError('Reviewer input exceeds 1 MiB', 'usage_error')
    result, seen = [], set()
    for raw in values or []:
        for value in raw.split(','):
            value = value.strip()
            validate(value)
            if value not in seen:
                seen.add(value); result.append(value)
                if len(result) > 100:
                    raise GVError('At most 100 reviewer handles per creation', 'usage_error')
    return result


def validate(value):
    from .metadata import validate_reviewer
    if '/' not in value:
        if len(value) > 205:
            raise GVError('Reviewer handle is too long', 'usage_error')
        validate_reviewer(value)
        return
    parts = value.split('/')
    if (len(parts) != 2 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', parts[0])
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}', parts[1])):
        raise GVError('Use a reviewer login, ORG/TEAM-SLUG or team:ID', 'usage_error')


def resolve(values, client):
    from .directory import user_value, team_value
    from .metadata import requested_reviewers
    result, catalogs = [], {}
    for value in values:
        if '/' not in value:
            rows = requested_reviewers([value], client)
        else:
            org, slug = value.split('/')
            key = org.casefold()
            if key not in catalogs:
                user_value(client.request('GET', 'users/' + quote(org, safe='')), org, True)
                teams, ids, slugs = [], set(), set()
                for page in range(1, 101):
                    batch = client.request('GET', 'orgs/' + quote(org, safe='') + '/teams',
                                           {'page': page, 'per_page': 100})
                    if not isinstance(batch, list):
                        raise GVError('Invalid team catalog', 'unexpected_response')
                    if len(batch) > 100:
                        raise GVError('Team page exceeds requested size', 'pagination_limit')
                    if not batch:
                        break
                    for row in batch:
                        team_value(row)
                        if row['id'] in ids or row['slug'] in slugs:
                            raise GVError('Repeated or ambiguous team catalog', 'pagination_error')
                        teams.append(row); ids.add(row['id']); slugs.add(row['slug'])
                else:
                    raise GVError('Team catalog page bound reached', 'pagination_limit')
                catalogs[key] = teams
            matches = [t for t in catalogs[key] if t['slug'] == slug]
            if not matches:
                raise GVError('Team is absent from the visible organization catalog; eligibility is unknown', 'team_not_found')
            rows = [{'id': matches[0]['id'], 'is_team': True}]
        result.extend(rows)
    return list({(r['id'], r['is_team']): r for r in result}.values())


def preflight(web, client, repo, payload, rows, number=None):
    from .directory import user_value
    from .metadata import reviewer_preflight
    author = user_value(client.request('GET', 'user'))
    actor = web.request('GET', 'user')
    if (not isinstance(actor, dict) or type(actor.get('id')) is not int
            or actor['id'] != author['id'] or not isinstance(actor.get('username'), str)
            or actor['username'].casefold() != author['login'].casefold()):
        raise GVError('Public and web reviewer session identities differ or are incomplete', 'identity_mismatch')
    if any(not r['is_team'] and r['id'] == author['id'] for r in rows):
        raise GVError('The PR author cannot be requested as reviewer', 'usage_error')
    candidates = reviewer_preflight(web, repo, payload['base'], payload['head'], number)
    eligible = set()
    for field, is_team in (('users', False), ('teams', True)):
        batch = candidates[field]
        if len(batch) > 10000:
            raise GVError('Reviewer candidate bound reached', 'catalog_limit')
        seen = set()
        for row in batch:
            if (not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0
                    or row['id'] in seen):
                raise GVError('Malformed or duplicate reviewer candidate', 'unexpected_response')
            seen.add(row['id'])
            if not is_team and row.get('isBot', False) is not False:
                continue
            eligible.add((row['id'], is_team))
    if not {(r['id'], r['is_team']) for r in rows} <= eligible:
        raise GVError('Requested reviewer is not in the scoped candidate response', 'reviewer_not_eligible')
    # Candidate visibility is a preflight observation, not a permission guarantee.
