import copy
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.commit_search import graph_complete, search_commits

A, B, C, D = (c * 40 for c in 'abcd')


def row(identifier=A, parents=(B,), message='Fix Привет\nbody'):
    return {'sha': identifier, 'parents': [{'sha': p} for p in parents], 'commit': {'message': message}}


class Fake:
    backend = 'public'
    dry_run = False

    def __init__(self):
        self.calls = []; self.head = row(); self.pages = [[row(), row(B, (), 'Initial commit')]]

    def request(self, method, path, query=None, **kwargs):
        self.calls.append((method, path, query, kwargs))
        if path == 'repos/me/demo': return {'default_branch': 'master'}
        if path.endswith('/branches'):
            return [{'name': 'feature/one', 'commit': {'sha': A}}] if query['page'] == 1 else []
        if '/commits/' in path: return copy.deepcopy(self.head)
        return copy.deepcopy(self.pages[query['page'] - 1])


class CommitSearchTests(unittest.TestCase):
    def setUp(self): self.client = Fake()

    def call(self, query='привет', *args):
        return search_commits(self.client, 'me/demo', parser().parse_args(['search', 'commits', query, *args]))

    def test_default_branch_pin_message_match_and_graph_coverage(self):
        result = self.call()
        self.assertEqual(result['commit_sha'], A)
        self.assertEqual(result['total_matches'], 1)
        self.assertEqual(result['results'][0]['sha'], A)
        self.assertTrue(result['coverage']['parent_graph_closed'])
        self.assertFalse(result['coverage']['global_search'])
        self.assertFalse(result['coverage']['git_object_hashes_verified'])
        self.assertEqual(self.client.calls[-1][2], {'sha': A, 'page': 1, 'per_page': 50})
        self.assertTrue(self.client.calls[-1][3]['require_complete'])

    def test_full_sha_and_case_sensitive_message_only(self):
        self.assertEqual(self.call('привет', '--ref', A, '--case-sensitive')['total_matches'], 0)
        self.assertEqual(self.call('Привет', '--ref', A, '--case-sensitive')['total_matches'], 1)
        self.assertEqual(self.call(A, '--ref', A)['total_matches'], 0)

    def test_slash_branch_resolves_through_catalog(self):
        self.assertEqual(self.call('Fix', '--ref', 'feature/one')['commit_sha'], A)
        self.assertTrue(any(c[1].endswith('/branches') for c in self.client.calls))

    def test_capped_short_page_continues_until_graph_closure(self):
        self.client.pages = [[row()], [row(B, (), 'Initial')]]
        result = self.call('Initial', '--ref', A, '--page-size', '1')
        self.assertEqual(result['results'][0]['sha'], B)
        self.assertEqual(result['coverage']['pages_read'], 2)

    def test_merge_graph_all_reachable_parents_included(self):
        self.client.head = row(A, (B, C))
        self.client.pages = [[self.client.head, row(B, (D,)), row(C, (D,)), row(D, (), 'root')]]
        self.assertEqual(self.call('root', '--ref', A)['coverage']['commits_scanned'], 4)

    def test_closed_graph_ends_without_requesting_unknown_extra_page(self):
        result = self.call('Initial', '--ref', A, '--max-pages', '1')
        self.assertEqual(result['total_matches'], 1)
        self.assertEqual(len([c for c in self.client.calls if c[1].endswith('/commits')]), 1)

    def test_incomplete_history_never_returns_early_matches(self):
        self.client.pages = [[row()], []]
        with self.assertRaises(GVError) as cm: self.call('Fix', '--ref', A)
        self.assertEqual(cm.exception.code, 'incomplete_history')

    def test_page_entry_and_match_bounds_fail_without_partial_results(self):
        self.client.pages = [[row()], [row(B, (), 'Fix root')]]
        with self.assertRaises(GVError) as cm: self.call('Fix', '--ref', A, '--max-pages', '1')
        self.assertEqual(cm.exception.code, 'pagination_limit')
        with self.assertRaises(GVError) as cm: self.call('Fix', '--ref', A, '--max-entries', '1')
        self.assertEqual(cm.exception.code, 'search_limit')
        with self.assertRaises(GVError) as cm: self.call('Fix', '--ref', A, '--max-matches', '1')
        self.assertEqual(cm.exception.code, 'search_limit')

    def test_repeated_sha_across_pages_refuses(self):
        self.client.pages = [[row()], [row()]]
        with self.assertRaises(GVError) as cm: self.call('Fix', '--ref', A)
        self.assertEqual(cm.exception.code, 'pagination_error')

    def test_unrelated_history_is_not_a_complete_search(self):
        self.client.pages[0].append(row(C, (), 'unrelated'))
        with self.assertRaises(GVError) as cm: self.call('unrelated', '--ref', A)
        self.assertEqual(cm.exception.code, 'unexpected_history')

    def test_head_mismatch_or_list_metadata_change_refuses(self):
        for head in (row(C, (B,)), row(A, (B,), 'changed')):
            self.client = Fake(); self.client.head = head
            with self.assertRaises(GVError): self.call('Fix', '--ref', A)

    def test_wrong_first_list_commit_refuses(self):
        self.client.pages = [[row(B, (), 'root'), row()]]
        with self.assertRaises(GVError) as cm: self.call('Fix', '--ref', A)
        self.assertEqual(cm.exception.code, 'unexpected_history')

    def test_missing_message_parents_and_invalid_sha_refuse(self):
        for bad in ({'sha': A}, row('bad'), {**row(), 'parents': None}, {**row(), 'parents': [{}]}, {**row(), 'commit': {'message': None}}):
            self.client = Fake(); self.client.pages = [[bad]]
            with self.assertRaises(GVError): self.call('Fix', '--ref', A)

    def test_self_duplicate_parents_and_cycle_refuse(self):
        for parents in ((A,), (B, B)):
            self.client = Fake(); self.client.head = row(A, parents)
            with self.assertRaises(GVError): self.call('Fix', '--ref', A)
        self.client = Fake(); self.client.pages = [[row(), row(B, (A,))]]
        with self.assertRaises(GVError): self.call('Fix', '--ref', A)

    def test_invalid_query_and_page_size_before_network(self):
        for query, flags in (('', []), ('x\ny', []), (' ', []), ('query', ['--page-size', '51'])):
            with self.assertRaises(GVError): self.call(query, *flags)
        self.assertEqual(self.client.calls, [])

    def test_dry_run_no_network_or_sha_guess(self):
        self.client.dry_run = True
        result = self.call('Fix', '--ref', 'feature/one', '--dry-run')
        self.assertEqual(result['ref'], 'feature/one')
        self.assertNotIn('commit_sha', result); self.assertEqual(self.client.calls, [])

    def test_large_graph_uses_no_recursion(self):
        identifiers = [f'{i:040x}' for i in range(1, 2002)]
        rows = {identifier: {'parents': [identifiers[i+1]] if i+1 < len(identifiers) else []} for i, identifier in enumerate(identifiers)}
        self.assertTrue(graph_complete(rows, identifiers[0]))

    def test_cli_dispatch_and_public_guard(self):
        with patch('gitverse_cli.cli.Client', return_value=self.client):
            self.assertEqual(execute(parser().parse_args(['search', 'commits', 'Fix', '-R', 'me/demo']))['total_matches'], 1)
            self.client.backend = 'web'
            with self.assertRaises(GVError): execute(parser().parse_args(['search', 'commits', 'Fix', '-R', 'me/demo', '--backend', 'web']))
        self.assertTrue(all(c[0] == 'GET' for c in self.client.calls))
