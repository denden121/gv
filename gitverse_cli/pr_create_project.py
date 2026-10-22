"""Project planning and read-only resolution; PR assignment remains disabled."""
import csv
import http.client
import math
import time

from .client import GVError


def project_titles(values):
    if values is None:
        return []
    if not isinstance(values, list) or not values or len(values) > 100:
        raise GVError('Pass 1..100 project titles, repeated or comma-separated', 'usage_error')
    result = []
    for value in values:
        try:
            valid = (isinstance(value, str) and len(value.encode('utf-8')) <= 1048576
                     and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value))
        except UnicodeError:
            valid = False
        if not valid:
            raise GVError('Project input must be UTF-8 without controls, at most 1 MiB', 'usage_error')
        try:
            titles = next(csv.reader([value], strict=True))
        except (csv.Error, StopIteration):
            raise GVError('Use project titles as a valid single CSV record', 'usage_error') from None
        if not titles or len(result) + len(titles) > 100:
            raise GVError('Pass 1..100 project titles, repeated or comma-separated', 'usage_error')
        for title in titles:
            if not title.strip() or len(title.encode('utf-8')) > 1024:
                raise GVError('Project titles must be nonblank and at most 1024 UTF-8 bytes', 'usage_error')
        # Preserve literal titles, order, case and repeats. No remote identity
        # resolution or deduplication can honestly be asserted by this plan.
        result.extend(titles)
    return result


class ProjectResolver:
    """Internal repository catalog preflight, not PR membership verification.

    Deliberately not wired into create_pull until assignment and readback are
    established. Resolving a title must never enable a speculative PR write.
    Only exact titles are matched; GitHub case matching is not inferred.
    """

    def __init__(self, client, repo, deadline, *, max_pages=100, max_entries=10000):
        from .repositories import component
        parts = repo.split('/') if isinstance(repo, str) else []
        if len(parts) != 2:
            raise GVError('Use an explicit OWNER/REPO for project resolution', 'usage_error')
        self.repo = '/'.join(component(p) for p in parts)
        if (type(max_pages) is not int or not 1 <= max_pages <= 100
                or type(max_entries) is not int or not 1 <= max_entries <= 10000):
            raise GVError('Project resolution bounds must be 1..100 pages and 1..10000 entries', 'usage_error')
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise GVError('Project resolution requires a finite deadline', 'usage_error')
        if client.backend != 'public' or client.dry_run:
            raise GVError('Project resolution requires a live public read client', 'usage_error')
        self.client, self.deadline = client, deadline
        self.max_pages, self.max_entries = max_pages, max_entries

    def _get(self, path, query=None):
        if time.monotonic() >= self.deadline:
            raise GVError('Project resolution deadline reached', 'deadline_exceeded')
        try:
            result = self.client.request('GET', path, query=query, deadline=self.deadline,
                                         require_complete=True, max_response_bytes=33554432,
                                         expected_status=200)
        except http.client.HTTPException as error:
            raise GVError('Project catalog response interrupted', 'network_error') from error
        if time.monotonic() >= self.deadline:
            raise GVError('Project resolution deadline reached', 'deadline_exceeded')
        return result

    def _repository(self):
        from .repositories import identity
        row = identity(self._get('repos/' + self.repo), self.repo)
        return row['id'], row['full_name'].casefold(), row['private']

    def _catalog(self):
        result, seen = {}, set()
        for page in range(1, self.max_pages + 1):
            rows = self._get('repos/' + self.repo + '/projects',
                             {'state': 'all', 'page': page, 'limit': 100})
            if not isinstance(rows, list):
                raise GVError('Expected a project array', 'unexpected_response')
            if len(rows) > 100 or len(seen) + len(rows) > self.max_entries:
                raise GVError('Project catalog entry limit reached', 'pagination_limit')
            if not rows:
                return result
            for row in rows:
                if (not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0
                        or row.get('state') not in ('open', 'closed')
                        or type(row.get('private')) is not bool):
                    raise GVError('Malformed project identity/state', 'unexpected_response')
                title = row.get('title')
                try:
                    valid = (isinstance(title, str) and bool(title.strip())
                             and len(title.encode('utf-8')) <= 1024
                             and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in title))
                except UnicodeError:
                    valid = False
                if not valid:
                    raise GVError('Malformed project title', 'unexpected_response')
                if row['id'] in seen:
                    raise GVError('Repeated project identity; catalog is incomplete', 'pagination_error')
                seen.add(row['id'])
                result.setdefault(title, []).append((row['id'], title, row['state'], row['private']))
        raise GVError('Project catalog page limit reached before empty page', 'pagination_limit')

    @staticmethod
    def _select(catalog, titles):
        result = []
        for title in titles:
            matches = catalog.get(title, [])
            if not matches:
                raise GVError('Requested project title is not visible in this repository', 'project_not_found')
            if len(matches) != 1:
                raise GVError('Requested project title is ambiguous', 'ambiguous_project')
            if matches[0][2] != 'open':
                raise GVError('Requested project is closed', 'project_closed')
            result.append(matches[0])
        return result

    def resolve(self, values):
        """Resolve repeated/CSV flag values, preserving order and repetitions.

        Two full bounded scans and repository identity checks detect observed
        changes. There is no atomic snapshot, permission-to-write inference,
        membership verification or remote mutation.
        """
        try:
            titles = project_titles(values)
            if not titles:
                raise GVError('At least one project title is required', 'usage_error')
            repository = self._repository()
            selected = self._select(self._catalog(), titles)
            fresh = self._catalog()
            if any(fresh.get(title) != [pin] for title, pin in zip(titles, selected)):
                raise GVError('Requested projects changed during resolution', 'concurrent_change')
            if self._repository() != repository:
                raise GVError('Repository changed during project resolution', 'concurrent_change')
            return {'repository': self.repo, 'repository_id': repository[0],
                    'projects': [{'id': pin[0], 'title': pin[1]} for pin in selected],
                    'catalog_complete': True, 'atomic': False,
                    'assignment_verified': False, 'mutation_attempted': False,
                    'mutation_outcome': 'none'}
        except GVError as error:
            error.details = {'repository': self.repo, 'stage': 'project_resolution',
                             'mutation_attempted': False, 'mutation_outcome': 'none'}
            raise
