"""Bounded public-API source resolution for qualified PR heads."""
import re
from .client import GVError
from .directory import user_value
from .repositories import identity, source_name

MAX_PAGES = 100
PAGE_SIZE = 100
MAX_ENTRIES = 100000
MAX_REQUESTS = 1000
MAX_DEPTH = 10


def repository_name(row):
    if not isinstance(row, dict) or not isinstance(row.get('full_name'), str):
        raise GVError('Missing repository name in head resolution', 'unexpected_response')
    try:
        name = source_name(row['full_name'])
    except GVError:
        raise GVError('Invalid repository name in head resolution', 'unexpected_response') from None
    identity(row, name)
    return name


class Resolver:
    def __init__(self, client):
        self.client, self.entries, self.requests = client, 0, 0

    def get(self, path, query=None):
        self.requests += 1
        if self.requests > MAX_REQUESTS:
            raise GVError('Head resolution request bound reached', 'catalog_limit')
        return self.client.request('GET', path, query)

    def catalog(self, path, key):
        seen = set()
        for page in range(1, MAX_PAGES + 1):
            rows = self.get(path, {'page': page, 'per_page': PAGE_SIZE})
            if not isinstance(rows, list):
                raise GVError('Invalid head resolution catalog', 'unexpected_response')
            if len(rows) > PAGE_SIZE:
                raise GVError('Head resolution catalog exceeded the requested page size; no partial selection',
                              'pagination_limit')
            if not rows:
                return
            for row in rows:
                self.entries += 1
                if self.entries > MAX_ENTRIES:
                    raise GVError('Head resolution entry bound reached', 'catalog_limit')
                value = row.get(key) if isinstance(row, dict) else None
                if (key == 'id' and (type(value) is not int or value <= 0)
                        or key == 'name' and (not isinstance(value, str) or not value)):
                    raise GVError('Invalid head resolution entry', 'unexpected_response')
                if value in seen:
                    raise GVError('Repeated or shifting head resolution pages', 'pagination_error')
                seen.add(value)
                yield row
        raise GVError('Head resolution page bound reached; no partial selection', 'pagination_limit')

    def root(self, row):
        seen = set()
        for _ in range(MAX_DEPTH):
            name = repository_name(row)
            if row['id'] in seen:
                raise GVError('Cyclic fork ancestry', 'unexpected_response')
            seen.add(row['id'])
            if row.get('fork') is False:
                return row['id'], name.casefold()
            parent = row.get('parent')
            if row.get('fork') is not True or not isinstance(parent, dict):
                raise GVError('Fork ancestry is incomplete', 'unexpected_response')
            parent_name = repository_name(parent)
            current = identity(self.get('repos/' + parent_name), parent_name)
            if current['id'] != parent['id']:
                raise GVError('Fork ancestor changed', 'concurrent_change')
            row = current
        raise GVError('Fork ancestry depth bound reached', 'catalog_limit')

    def sha(self, repo, name):
        found = None
        for row in self.catalog('repos/' + repo + '/branches', 'name'):
            sha = (row.get('commit') or {}).get('sha') if isinstance(row.get('commit'), dict) else None
            if not isinstance(sha, str) or not re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', sha):
                raise GVError('Invalid branch SHA in head resolution', 'unexpected_response')
            if row['name'] == name:
                found = sha.lower()
        return found

    def resolve(self, repo, repository, owner, author, head, base):
        profile = user_value(self.get('users/' + owner), owner)
        if profile.get('type') != 'User':
            raise GVError('Qualified --head requires a user account', 'usage_error')
        root = self.root(repository)
        own = profile['id'] == author['id'] and profile['login'].casefold() == author['login'].casefold()
        path = 'user/repos' if own else 'users/' + owner + '/repos'
        matches, names = [], set()
        for row in self.catalog(path, 'id'):
            name = repository_name(row)
            user = user_value(row.get('owner'))
            if name.split('/')[0].casefold() != user['login'].casefold():
                raise GVError('Source repository namespace mismatch', 'unexpected_response')
            if name.casefold() in names:
                raise GVError('Repeated source repository name', 'pagination_error')
            names.add(name.casefold())
            # Observed user/repos also includes accessible organization repos.
            # Filter those by validated namespace, never by a guessed name.
            if user['login'].casefold() != owner.casefold():
                continue
            if user['id'] != profile['id'] or user.get('type') != 'User':
                raise GVError('Source repository owner identity mismatch', 'unexpected_response')
            if type(row.get('fork')) is not bool:
                raise GVError('Unknown source fork status', 'unexpected_response')
            if row['fork'] and self.root(row) == root:
                current = identity(self.get('repos/' + name), name)
                if current['id'] != row['id'] or self.root(current) != root or current.get('owner') != row.get('owner'):
                    raise GVError('Source repository changed during selection', 'concurrent_change')
                sha = self.sha(name, head)
                if sha is not None:
                    matches.append((current, sha))
        if len(matches) != 1:
            raise GVError('Qualified source must resolve to one visible fork containing the exact branch; other users catalog exposes public repositories only',
                          'ambiguous_head' if matches else 'head_not_visible',
                          details={'visible_matches': len(matches), 'private_source_visibility': own})
        source, sha = matches[0]
        base_sha = self.sha(repo, base)
        if base_sha is None:
            raise GVError('Base branch not visible in complete catalog', 'head_not_visible')
        return {'repository': source['full_name'], 'id': source['id'], 'head_sha': sha,
                'base_sha': base_sha, 'owner_id': profile['id'],
                'head_repo': source['full_name'].split('/')[1]}
