import copy
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.item_search import search_items


def row(number=1, **values):
    return {'id': number, 'number': number, 'title': 'Fix Привет', 'body': 'literal body', 'state': 'open',
            'merged': False, 'user': {'login': 'Alice'}, 'assignees': [{'login': 'Bob'}], 'labels': [{'name': 'bug'}], **values}


class Fake:
    backend = 'public'
    dry_run = False
    def __init__(self):
        self.calls = []; self.pages = {'o/a': [[row()], []], 'o/b': [[row(2, body='second repo')], []]}
    def request(self, method, path, query=None, **kwargs):
        self.calls.append((method, path, query, kwargs))
        if path == 'user': return {'id': 3, 'login': 'Alice'}
        repo = '/'.join(path.split('/')[1:3])
        return copy.deepcopy(self.pages[repo][query['page'] - 1])


class ItemSearchTests(unittest.TestCase):
    def setUp(self): self.client = Fake()
    def call(self, kind='prs', query='привет', *flags):
        return search_items(self.client, parser().parse_args(['search', kind, query, *flags]))

    def test_multi_repo_results_keep_repository_identity_and_coverage(self):
        result = self.call('prs', 'Привет', '--repos', 'o/a', '--repos', 'o/b')
        self.assertEqual(result['total_matches'], 2)
        self.assertEqual([r['repository'] for r in result['results']], ['o/a', 'o/b'])
        self.assertEqual(result['coverage']['entries_scanned'], 2)
        self.assertFalse(result['coverage']['atomic_snapshot'])
        self.assertTrue(all(c[2]['state'] == 'all' for c in self.client.calls))
        self.assertEqual(len({c[3]['deadline'] for c in self.client.calls}), 1)

    def test_single_R_context_scope_and_case_sensitive(self):
        self.assertEqual(self.call('prs', 'привет', '-R', 'o/a')['total_matches'], 1)
        self.assertEqual(self.call('prs', 'привет', '-R', 'o/a', '--case-sensitive')['total_matches'], 0)
        with patch('gitverse_cli.cli.repository', return_value='o/b'):
            self.assertEqual(self.call('prs', 'second')['results'][0]['repository'], 'o/b')

    def test_deduplicate_repository_scopes_casefold(self):
        result = self.call('prs', 'Fix', '--repos', 'o/a', '--repos', 'O/A')
        self.assertEqual(result['repositories'], ['o/a'])
        self.assertEqual(len(self.client.calls), 2)

    def test_and_author_assignee_labels_filters_and_at_me_once(self):
        result = self.call('prs', 'body', '-R', 'o/a', '--author', '@me', '--assignee', 'bob', '--label', 'bug')
        self.assertEqual(result['total_matches'], 1)
        self.assertEqual(result['filters']['author'], 'Alice')
        self.assertEqual([c[1] for c in self.client.calls].count('user'), 1)
        self.assertEqual(self.call('prs', 'body', '-R', 'o/a', '--label', 'bug,extra')['total_matches'], 0)

    def test_nullable_metadata_means_unassigned_unlabelled(self):
        self.client.pages['o/a'] = [[row(assignees=None, labels=None, body=None)], []]
        self.assertEqual(self.call('prs', 'Fix', '-R', 'o/a', '--assignee', 'bob')['total_matches'], 0)
        self.assertEqual(self.call('prs', 'Fix', '-R', 'o/a', '--label', 'bug')['total_matches'], 0)

    def test_issue_search_excludes_pr_discriminator(self):
        self.client.pages['o/a'] = [[row(pull_request={}), row(2, pull_request=None)], []]
        result = self.call('issues', 'Fix', '-R', 'o/a')
        self.assertEqual([r['number'] for r in result['results']], [2])
        self.assertEqual(result['coverage']['repositories'][0]['pull_requests_excluded'], 1)

    def test_closed_includes_merged_and_merged_is_pr_only(self):
        self.client.pages['o/a'] = [[row(state='closed', merged=True), row(2, state='closed'), row(3)], []]
        self.assertEqual(self.call('prs', 'Fix', '-R', 'o/a', '--state', 'closed')['total_matches'], 2)
        self.assertEqual(self.call('prs', 'Fix', '-R', 'o/a', '--state', 'merged')['total_matches'], 1)
        self.assertEqual(self.call('prs', 'Fix', '-R', 'o/a', '--state', 'open')['total_matches'], 1)

    def test_unknown_filter_metadata_not_silently_excluded(self):
        for field, value, flags in (('user', None, ['--author', 'alice']), ('assignees', {}, ['--assignee', 'bob']),
                                    ('labels', [{}], ['--label', 'bug']), ('body', 3, []), ('merged', None, [])):
            self.client.pages['o/a'] = [[row(**{field: value})], []]
            with self.assertRaises(GVError): self.call('prs', 'not-matching', '-R', 'o/a', *flags)

    def test_short_pages_continue_until_empty(self):
        self.client.pages['o/a'] = [[row()], [row(2)], []]
        self.assertEqual(self.call('prs', 'Fix', '-R', 'o/a')['total_matches'], 2)

    def test_duplicate_ids_numbers_and_bad_shapes_refuse(self):
        for pages in ([[row()], [row()]], [[row(), row(2, id=1)], []], [[row(id=True)], []], [{'items': []}]):
            self.client.pages['o/a'] = pages
            with self.assertRaises(GVError): self.call('prs', 'Fix', '-R', 'o/a')

    def test_page_entry_match_and_scope_bounds_fail_without_partial_results(self):
        for flags, code in ((['--max-pages', '1'], 'pagination_limit'), (['--max-entries', '1'], 'search_limit'),
                            (['--max-matches', '1'], 'search_limit'), (['--max-repos', '1'], 'search_limit')):
            with self.assertRaises(GVError) as cm: self.call('prs', 'Fix', '--repos', 'o/a', '--repos', 'o/b', *flags)
            self.assertEqual(cm.exception.code, code)

    def test_second_repo_error_does_not_return_first_repo_results(self):
        original = self.client.request
        def request(method, path, *args, **kwargs):
            if 'o/b' in path: raise GVError('denied', 'http_error', 403)
            return original(method, path, *args, **kwargs)
        self.client.request = request
        with self.assertRaises(GVError) as cm: self.call('prs', 'Fix', '--repos', 'o/a', '--repos', 'o/b')
        self.assertEqual(cm.exception.status, 403)

    def test_invalid_inputs_before_network(self):
        for flags in (['-R', 'o/a', '--repos', 'o/b'], ['--repos', 'https://gitverse.ru/o/a'],
                      ['-R', 'o/a', '--author', 'a/b'], ['-R', 'o/a', '--label', ''], ['--repos', 'o/a', '--max-repos', '101']):
            with self.assertRaises(GVError): self.call('prs', 'Fix', *flags)
        self.assertEqual(self.client.calls, [])

    def test_dry_run_does_not_resolve_at_me_or_network(self):
        self.client.dry_run = True
        result = self.call('issues', 'Fix', '--repos', 'o/a', '--author', '@me')
        self.assertTrue(result['dry_run']); self.assertEqual(result['filters']['author'], '@me')
        self.assertEqual(self.client.calls, [])

    def test_cli_dispatch_and_public_guard(self):
        with patch('gitverse_cli.cli.Client', return_value=self.client):
            self.assertEqual(execute(parser().parse_args(['search', 'prs', 'Fix', '-R', 'o/a']))['total_matches'], 1)
            self.client.backend = 'web'
            with self.assertRaises(GVError): execute(parser().parse_args(['search', 'issues', 'Fix', '-R', 'o/a', '--backend', 'web']))
        self.assertTrue(all(c[0] == 'GET' for c in self.client.calls))
