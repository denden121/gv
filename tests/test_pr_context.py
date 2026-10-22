import copy
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.pr_context import collect_context


class Fake:
    backend = 'public'
    dry_run = False

    def __init__(self):
        self.calls = []
        self.reads = 0
        self.change = None
        self.pull = {'id': 7, 'number': 9, 'state': 'open', 'merged': False, 'updated_at': 'v1',
                     'head': {'sha': 'a' * 40, 'ref': 'topic', 'repo_id': 2},
                     'base': {'sha': 'b' * 40, 'ref': 'main', 'repo_id': 2}, 'changed_files': 1}
        self.data = {'files': [[{'filename': 'a.txt', 'patch': '@@ -1 +1 @@\n-old\n+new'}], []],
                     'reviews': [[{'id': 3}], []], 'commits': [[{'sha': 'a' * 40}], []], 'comments': [{'id': 4, 'body': 'text'}]}

    def request(self, method, path, query=None, **kwargs):
        self.calls.append((method, path, query, kwargs))
        if path.endswith('/pulls/9'):
            self.reads += 1
            if self.reads == 2 and self.change: self.change(self.pull)
            return copy.deepcopy(self.pull)
        kind = path.rsplit('/', 1)[-1]
        data = self.data[kind]
        return copy.deepcopy(data if kind == 'comments' else data[min(query['page'] - 1, len(data) - 1)])


class ContextTests(unittest.TestCase):
    def setUp(self): self.client = Fake()

    def call(self, *flags):
        args = parser().parse_args(['pr', 'context', '9', '-R', 'o/r', *flags])
        args.number = 9
        return collect_context(self.client, 'o/r', args)

    def assert_error(self, code, *flags):
        with self.assertRaises(GVError) as cm: self.call(*flags)
        self.assertEqual(cm.exception.code, code)

    def test_complete_context_pins_head_base_and_shared_deadline(self):
        result = self.call('--require-patches')
        self.assertEqual(result['head_sha'], 'a' * 40)
        self.assertEqual(result['base_sha'], 'b' * 40)
        self.assertEqual(result['coverage']['entries_scanned'], 4)
        self.assertTrue(result['coverage']['complete'])
        self.assertFalse(result['coverage']['atomic_snapshot'])
        self.assertFalse(result['coverage']['patch_integrity_verified'])
        self.assertEqual(self.client.reads, 2)
        self.assertEqual(len({c[3]['deadline'] for c in self.client.calls}), 1)
        self.assertTrue(all(c[0] == 'GET' and c[3]['require_complete'] and c[3]['max_response_bytes'] == 33554432 for c in self.client.calls))

    def test_short_pages_continue_to_empty_and_review_limit(self):
        self.client.pull['changed_files'] = 2
        self.client.data['files'].insert(1, [{'filename': 'b.txt', 'patch': 'text'}])
        result = self.call('--page-size', '30')
        self.assertEqual(len(result['files']), 2)
        self.assertEqual(result['coverage']['catalogs']['files']['requests'], 3)
        queries = {p: q for _, p, q, _ in self.client.calls}
        self.assertEqual(queries['repos/o/r/pulls/9/reviews']['limit'], 30)
        self.assertEqual(queries['repos/o/r/pulls/9/files']['per_page'], 30)
        self.assertIsNone(queries['repos/o/r/issues/9/comments'])

    def test_missing_patch_explicit_or_strict_refusal(self):
        for value in (None, ''):
            self.client.data['files'][0][0]['patch'] = value
            self.assertEqual(self.call()['missing_patches'], ['a.txt'])
            self.assert_error('incomplete_diff', '--require-patches')

    def test_empty_context_supported(self):
        self.client.pull['changed_files'] = 0
        self.client.data = {'files': [[]], 'reviews': [[]], 'commits': [[]], 'comments': []}
        self.assertEqual(self.call()['coverage']['entries_scanned'], 0)

    def test_head_base_branch_and_metadata_races_refused(self):
        for change in (lambda p: p['head'].update(sha='c' * 40), lambda p: p['base'].update(sha='c' * 40),
                       lambda p: p['head'].update(repo_id=3), lambda p: p['base'].update(ref='other'),
                       lambda p: p.update(id=8), lambda p: p.update(updated_at='v2'),
                       lambda p: p.update(state='closed'), lambda p: p.update(title='changed')):
            self.client = Fake(); self.client.change = change
            self.assert_error('concurrent_change')

    def test_unrelated_repository_update_does_not_invalidate(self):
        self.client.pull['head']['repo'] = {'id': 2, 'full_name': 'o/r', 'updated_at': 'v1'}
        self.client.change = lambda p: p['head']['repo'].update(updated_at='v2')
        self.assertTrue(self.call()['coverage']['complete'])

    def test_invalid_parent_identity_and_version(self):
        for key, value in [('id', True), ('number', 8), ('merged', None), ('updated_at', ''), ('head', {}), ('head', [1]), ('base', {}), ('state', 'unknown')]:
            self.client = Fake(); self.client.pull[key] = value
            self.assert_error('unexpected_response')
            self.assertEqual(len(self.client.calls), 1)

    def test_file_count_unknown_and_mismatch(self):
        for value in (None, True, -1):
            self.client.pull['changed_files'] = value
            self.assert_error('unexpected_response')
        self.client.pull['changed_files'] = 2
        self.assert_error('incomplete_context')

    def test_malformed_catalogs_and_row_identities(self):
        for kind, value in [('files', [[{}], []]), ('files', [[{'filename': 'a', 'patch': 5}], []]),
                            ('reviews', [[{'id': True}], []]), ('commits', [[{'sha': 'bad'}], []]),
                            ('comments', {'items': []}), ('comments', [None]), ('files', [{'items': []}])]:
            self.client = Fake(); self.client.data[kind] = value
            self.assert_error('unexpected_response')

    def test_repeated_pages_and_casefold_commit_duplicate(self):
        for kind in ('files', 'reviews', 'commits'):
            self.client = Fake(); self.client.data[kind] = [self.client.data[kind][0]]
            self.assert_error('pagination_error')
        self.client = Fake(); self.client.data['commits'][0].append({'sha': 'A' * 40})
        self.assert_error('pagination_error')
        self.client = Fake(); self.client.data['comments'].append({'id': 4})
        self.assert_error('pagination_error')

    def test_combined_entry_and_page_bounds_no_partial_context(self):
        self.assert_error('context_limit', '--max-entries', '3')
        self.client = Fake()
        self.assert_error('pagination_limit', '--max-pages', '1')

    def test_network_error_propagates_without_catalog_retry(self):
        with patch.object(self.client, 'request', side_effect=GVError('offline', 'network_error')) as request:
            self.assert_error('network_error')
            self.assertEqual(request.call_count, 1)

    def test_dry_run_no_network(self):
        self.client.dry_run = True
        self.assertTrue(self.call()['dry_run'])
        self.assertEqual(self.client.calls, [])

    def test_cli_dispatch_and_public_guard(self):
        with patch('gitverse_cli.cli.Client', return_value=self.client):
            self.assertTrue(execute(parser().parse_args(['pr', 'context', '9', '-R', 'o/r']))['coverage']['complete'])
        self.client.backend = 'web'; self.client.calls = []
        with patch('gitverse_cli.cli.Client', return_value=self.client), self.assertRaises(GVError) as cm:
            execute(parser().parse_args(['pr', 'context', '9', '-R', 'o/r', '--backend', 'web']))
        self.assertEqual(cm.exception.code, 'backend_unsupported'); self.assertEqual(self.client.calls, [])
