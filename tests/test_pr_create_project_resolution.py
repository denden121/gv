"""Read-only resolver contracts; no PR project-write behavior is claimed."""
import copy
import http.client
import io
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error

from gitverse_cli.client import Client, GVError
from gitverse_cli.pr_create_project import ProjectResolver


def project(i=7, title='Roadmap', **extra):
    return dict(id=i, title=title, state='open', private=True, **extra)


class Catalog:
    backend = 'public'
    dry_run = False

    def __init__(self):
        self.calls = []
        self.rows = [project()]
        self.repo = {'id': 3, 'full_name': 'owner/repo', 'private': True}
        self.scans = 0
        self.hook = lambda path, query, row: row

    def request(self, method, path, query=None, **kwargs):
        self.calls.append((method, path, query, kwargs))
        if method != 'GET':
            raise AssertionError('Resolver attempted a write')
        if path == 'repos/owner/repo':
            row = self.repo
        elif path == 'repos/owner/repo/projects':
            if query['page'] == 1:
                self.scans += 1
            start = (query['page'] - 1) * 100
            row = self.rows[start:start + 100]
        else:
            raise AssertionError('Untrusted path: ' + path)
        return self.hook(path, query, copy.deepcopy(row))


class ProjectResolutionTests(unittest.TestCase):
    def setUp(self):
        self.client = Catalog()

    def resolve(self, values=None, **kwargs):
        return ProjectResolver(self.client, 'owner/repo', time.monotonic() + 60,
                               **kwargs).resolve(['Roadmap'] if values is None else values)

    def error(self, code, values=None, **kwargs):
        with self.assertRaises(GVError) as caught:
            self.resolve(values, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.details['mutation_outcome'], 'none')
        self.assertFalse(caught.exception.details['mutation_attempted'])

    def test_exact_literals_numeric_titles_csv_repeats_and_scope(self):
        self.client.rows = [project(1, '123'), project(2, 'A,B'), project(3, ' Б ')]
        r = self.resolve(['123,"A,B"', ' Б ', '123'])
        self.assertEqual(r['projects'], [{'id': 1, 'title': '123'}, {'id': 2, 'title': 'A,B'},
                                         {'id': 3, 'title': ' Б '}, {'id': 1, 'title': '123'}])
        self.assertEqual(r['repository_id'], 3)
        self.assertTrue(r['catalog_complete'])
        self.assertFalse(r['assignment_verified'])
        self.assertFalse(r['atomic'])
        self.assertEqual(self.client.scans, 2)

    def test_case_does_not_guess_github_matching(self):
        self.error('project_not_found', ['roadmap'])

    def test_short_pages_continue_until_empty_on_both_scans(self):
        def hook(path, q, row):
            if path.endswith('/projects'):
                return [project(q['page'], 'Roadmap' if q['page'] == 2 else 'Other')] if q['page'] < 3 else []
            return row
        self.client.hook = hook
        self.assertEqual(self.resolve()['projects'][0]['id'], 2)
        self.assertEqual([c[2]['page'] for c in self.client.calls if c[1].endswith('/projects')], [1, 2, 3, 1, 2, 3])

    def test_full_page_requires_empty_following_page(self):
        self.client.rows = [project(i, str(i)) for i in range(1, 101)]
        self.resolve(['100'])
        self.assertEqual([c[2]['page'] for c in self.client.calls if c[1].endswith('/projects')], [1, 2, 1, 2])

    def test_missing_ambiguous_and_closed_are_not_success(self):
        self.client.rows = []
        self.error('project_not_found')
        self.client.rows = [project(1), project(2)]
        self.error('ambiguous_project')
        self.client.rows = [dict(project(), state='closed')]
        self.error('project_closed')

    def test_malformed_array_and_dtos(self):
        for bad in (None, {}, {'items': []}, [None], [True], [dict(project(), id=True)],
                    [dict(project(), id=0)], [dict(project(), title='')],
                    [dict(project(), title='a\x85b')], [dict(project(), title='\ud800')],
                    [dict(project(), title='a'*1025)], [dict(project(), state='unknown')],
                    [dict(project(), private=1)], [{'id': 7, 'title': 'Roadmap'}]):
            with self.subTest(bad=repr(bad)[:100]):
                self.client = Catalog()
                self.client.hook = lambda path, q, row: bad if path.endswith('/projects') else row
                self.error('unexpected_response')

    def test_duplicate_id_and_repeated_page_refuse(self):
        self.client.rows = [project(), project()]
        self.error('pagination_error')
        self.client.hook = lambda path, q, row: [project()] if path.endswith('/projects') else row
        self.error('pagination_error')

    def test_page_and_entry_and_oversized_page_limits(self):
        self.error('pagination_limit', max_pages=1)
        self.client.rows = [project(1), project(2, 'Other')]
        self.error('pagination_limit', max_entries=1)
        self.client.hook = lambda path, q, row: [project(i, str(i)) for i in range(101)] if path.endswith('/projects') else row
        self.error('pagination_limit')

    def test_selected_identity_title_state_privacy_changes_fail(self):
        for change in ({'id': 8}, {'title': 'Renamed'}, {'state': 'closed'}, {'private': False}):
            self.client = Catalog()
            def hook(path, q, row):
                if path.endswith('/projects') and self.client.scans == 2 and row:
                    row[0].update(change)
                return row
            self.client.hook = hook
            self.error('concurrent_change')

    def test_new_title_collision_on_second_scan_fails(self):
        def hook(path, q, row):
            return row + [project(8)] if path.endswith('/projects') and self.client.scans == 2 and row else row
        self.client.hook = hook
        self.error('concurrent_change')

    def test_unrelated_project_change_does_not_invalidate_selection(self):
        self.client.rows.append(project(8, 'Other'))
        def hook(path, q, row):
            if path.endswith('/projects') and self.client.scans == 2 and row:
                row[1]['title'] = 'Different'
            return row
        self.client.hook = hook
        self.assertEqual(self.resolve()['projects'], [{'id': 7, 'title': 'Roadmap'}])

    def test_repository_wrong_scope_malformed_and_changed_identity(self):
        for change in ({'full_name': 'other/repo'}, {'id': True}, {'private': None}):
            self.client = Catalog()
            self.client.repo.update(change)
            self.error('unexpected_response')
        for change in ({'id': 4}, {'private': False}):
            self.client = Catalog()
            def hook(path, q, row):
                if path == 'repos/owner/repo' and self.client.scans == 2:
                    row.update(change)
                return row
            self.client.hook = hook
            self.error('concurrent_change')

    def test_denied_and_missing_scopes_propagate_without_partial_result(self):
        for status in (401, 403, 404):
            self.client = Catalog()
            def hook(path, q, row):
                if path.endswith('/projects'):
                    raise GVError('Denied', 'http_error', status)
                return row
            self.client.hook = hook
            self.error('http_error')
            self.assertEqual(len(self.client.calls), 2)

    def test_deadline_prevents_network_and_checks_after_response(self):
        with patch('gitverse_cli.pr_create_project.time.monotonic', return_value=10):
            with self.assertRaises(GVError) as caught:
                ProjectResolver(self.client, 'owner/repo', 9).resolve(['Roadmap'])
        self.assertEqual(caught.exception.code, 'deadline_exceeded')
        self.assertEqual(self.client.calls, [])
        def hook(path, q, row):
            raise http.client.IncompleteRead(b'')
        self.client.hook = hook
        self.error('network_error')
        self.client.hook = lambda path, q, row: row
        with patch('gitverse_cli.pr_create_project.time.monotonic', side_effect=[1, 100]):
            with self.assertRaises(GVError) as caught:
                ProjectResolver(self.client, 'owner/repo', 50).resolve(['Roadmap'])
        self.assertEqual(caught.exception.code, 'deadline_exceeded')

    def test_transport_complete_status_size_deadline_and_no_returned_urls(self):
        self.client.rows[0]['url'] = 'https://elsewhere.invalid/steal'
        self.resolve()
        for method, path, q, options in self.client.calls:
            self.assertEqual(method, 'GET')
            self.assertTrue(path.startswith('repos/owner/repo'))
            self.assertTrue(options['require_complete'])
            self.assertEqual(options['expected_status'], 200)
            self.assertEqual(options['max_response_bytes'], 33554432)
            self.assertIsInstance(options['deadline'], float)
            if q:
                self.assertEqual(q['state'], 'all')
                self.assertEqual(q['limit'], 100)

    def test_redirect_never_forwards_credentials_to_another_origin(self):
        client = Client(dry_run=True, public_base='https://api.example.test')
        client.dry_run = False
        client.headers['Authorization'] = 'Bearer synthetic-test-only'
        client.opener = Mock()
        client.opener.open.side_effect = urllib.error.HTTPError(
            'https://api.example.test/repos/owner/repo', 302, 'Redirect',
            {'Location': 'https://elsewhere.invalid/steal'}, io.BytesIO(b''))
        with self.assertRaises(GVError) as caught:
            ProjectResolver(client, 'owner/repo', time.monotonic() + 60).resolve(['Roadmap'])
        self.assertEqual(caught.exception.status, 302)
        self.assertEqual(client.opener.open.call_count, 1)
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.example.test/repos/owner/repo')
        self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-test-only')

    def test_invalid_bounds_scope_or_backend_fail_without_network(self):
        for kwargs in ({'max_pages': True}, {'max_pages': 101}, {'max_entries': 0}, {'max_entries': 10001}):
            with self.assertRaises(GVError):
                ProjectResolver(self.client, 'owner/repo', 60, **kwargs)
        for repo in ('x', 'x/y/z', '../repo', 'https://example.invalid/r', None):
            with self.assertRaises(GVError):
                ProjectResolver(self.client, repo, 60)
        for deadline in (float('inf'), float('nan'), None, True):
            with self.assertRaises(GVError):
                ProjectResolver(self.client, 'owner/repo', deadline)
        for backend, dry in (('web', False), ('public', True)):
            self.client.backend, self.client.dry_run = backend, dry
            with self.assertRaises(GVError):
                ProjectResolver(self.client, 'owner/repo', 60)
        self.assertEqual(self.client.calls, [])

    def test_missing_or_invalid_inputs_fail_before_network(self):
        for values in (None, [], [''], ['bad\nname']):
            with self.assertRaises(GVError):
                ProjectResolver(self.client, 'owner/repo', time.monotonic() + 60).resolve(values)
        self.assertEqual(self.client.calls, [])


if __name__ == '__main__':
    unittest.main()
