import copy
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.run_list import list_runs


SHA = 'a' * 40
WF = {'workflow': 'ci.yml', 'name': 'Build', 'state': 'active'}


def run(number=1, **values):
    return {'id': number, 'ref': 'refs/heads/master', 'event': 'push', 'status': 'success',
            'actor': {'login': 'Alice', 'id': 3}, 'commit_sha': SHA, 'need_approval': False, **values}


class Fake:
    backend = 'public'
    dry_run = False

    def __init__(self):
        self.calls = []; self.pages = [{'workflow_runs': [run()], 'total_count': 1}]
        self.workflows = [{'workflows': [WF], 'total_count': 1}]
        self.workflow = WF

    def request(self, method, path, query=None, **kwargs):
        self.calls.append((method, path, query, kwargs))
        if path == 'user': return {'id': 3, 'login': 'Alice'}
        if path.endswith('/workflows'): return copy.deepcopy(self.workflows[query['page'] - 1])
        if '/workflows/' in path and not path.endswith('/runs'): return copy.deepcopy(self.workflow)
        return copy.deepcopy(self.pages[query['page'] - 1])


class RunListTests(unittest.TestCase):
    def setUp(self): self.client = Fake()

    def call(self, *argv):
        return list_runs(self.client, 'me/repo', parser().parse_args(['run', 'list', *argv]))

    def test_unfiltered_envelope_and_coverage(self):
        result = self.call()
        self.assertEqual(result['workflow_runs'][0]['id'], 1)
        self.assertEqual(result['total_count'], 1)
        self.assertFalse(result['coverage']['ci_outcome_verified'])
        self.assertFalse(result['coverage']['atomic_snapshot'])
        self.assertTrue(self.client.calls[0][3]['require_complete'])
        self.assertGreater(self.client.calls[0][3]['deadline'], 0)

    def test_all_filters_combined_and_no_untrusted_server_filters(self):
        result = self.call('-b', 'master', '-e', 'push', '-s', 'success', '-u', 'alice', '-c', SHA.upper())
        self.assertEqual(result['total_matches'], 1)
        self.assertEqual(self.client.calls[0][2], {'page': 1, 'per_page': 100})
        self.assertEqual(result['filters']['commit'], SHA)
        self.assertEqual(self.call('-e', 'workflow_dispatch')['total_matches'], 0)

    def test_filters_before_local_page_and_limit(self):
        self.client.pages = [{'workflow_runs': [run(1, event='push')], 'total_count': 3},
                             {'workflow_runs': [run(2, event='pull_request'), run(3, event='pull_request')], 'total_count': 3}]
        result = self.call('-e', 'pull_request', '--limit', '1', '--page', '2')
        self.assertEqual([r['id'] for r in result['workflow_runs']], [3])
        self.assertEqual(result['coverage']['entries_scanned'], 3)
        self.assertEqual(result['total_count'], 2)
        self.assertFalse(result['has_more'])

    def test_paginate_means_remaining_after_page(self):
        self.client.pages = [{'workflow_runs': [run(i) for i in range(1, 5)], 'total_count': 4}]
        result = self.call('--limit', '1', '--page', '3', '--paginate')
        self.assertEqual([r['id'] for r in result['workflow_runs']], [3, 4])

    def test_current_user_resolved_once(self):
        result = self.call('--user', '@me')
        self.assertEqual(result['filters']['user'], 'Alice')
        self.assertEqual([r[1] for r in self.client.calls].count('user'), 1)

    def test_branch_full_ref_and_tag_pull_exclusion(self):
        self.client.pages = [{'workflow_runs': [run(1, ref='refs/heads/feature/one'), run(2, ref='refs/tags/feature/one'), run(3, ref='refs/pull/1/head')], 'total_count': 3}]
        result = self.call('--branch', 'refs/heads/feature/one')
        self.assertEqual([r['id'] for r in result['workflow_runs']], [1])
        self.assertEqual(result['filters']['branch'], 'feature/one')

    def test_completed_includes_known_terminal_only(self):
        statuses = ['success', 'failure', 'cancelled', 'skipped', 'timed_out', 'running', 'waiting']
        rows = [run(i + 1, status=status) for i, status in enumerate(statuses)] + [run(8, need_approval=True)]
        self.client.pages = [{'workflow_runs': rows, 'total_count': len(rows)}]
        self.assertEqual([r['id'] for r in self.call('--status', 'completed')['workflow_runs']], [1, 2, 3, 4, 5])

    def test_unknown_terminal_classification_refused_but_exact_status_works(self):
        self.client.pages = [{'workflow_runs': [run(status='future_status')], 'total_count': 1}]
        with self.assertRaises(GVError) as cm: self.call('--status', 'completed')
        self.assertEqual(cm.exception.code, 'unknown_run_status')
        self.assertEqual(self.call('--status', 'future_status')['total_matches'], 1)

    def test_unknown_metadata_not_silently_excluded_even_when_other_filter_false(self):
        for field, value, flags in (('actor', None, ['--user', 'Alice']), ('ref', 'master', ['--branch', 'master']),
                                    ('commit_sha', 'abc', ['--commit', SHA]), ('event', None, ['--event', 'push']),
                                    ('status', None, ['--status', 'success']), ('need_approval', None, ['--status', 'completed'])):
            self.client.pages = [{'workflow_runs': [run(**{field: value})], 'total_count': 1}]
            with self.assertRaises(GVError): self.call('--event', 'nonexistent', *flags)

    def test_missing_metadata_can_be_listed_without_claiming_matching_filters(self):
        self.client.pages = [{'workflow_runs': [{'id': 1}], 'total_count': 1}]
        self.assertEqual(self.call()['total_count'], 1)

    def test_workflow_filename_uses_verified_scoped_endpoint(self):
        result = self.call('-w', '.gitverse/workflows/ci.yml')
        self.assertEqual(result['workflow'], 'ci.yml')
        self.assertEqual(result['coverage']['scope'], 'workflow')
        self.assertEqual(self.client.calls[-1][1], 'repos/me/repo/actions/workflows/ci.yml/runs')

    def test_workflow_exact_unique_display_name(self):
        self.assertEqual(self.call('--workflow', 'Build')['workflow'], 'ci.yml')
        self.assertEqual(self.client.calls[0][1], 'repos/me/repo/actions/workflows')

    def test_workflow_missing_ambiguous_identity_mismatch(self):
        with self.assertRaises(GVError) as cm: self.call('-w', 'missing')
        self.assertEqual(cm.exception.code, 'workflow_not_found')
        self.client.workflows = [{'workflows': [WF, {**WF, 'workflow': 'other.yml'}], 'total_count': 2}]
        with self.assertRaises(GVError) as cm: self.call('-w', 'Build')
        self.assertEqual(cm.exception.code, 'ambiguous_workflow')
        self.client.workflow = {**WF, 'workflow': 'wrong.yml'}
        with self.assertRaises(GVError): self.call('-w', 'ci.yml')

    def test_countless_short_pages_continue_until_empty(self):
        self.client.pages = [{'workflow_runs': [run(1)]}, {'workflow_runs': [run(2)]}, {'workflow_runs': []}]
        self.assertEqual(self.call('--paginate')['total_count'], 2)
        self.assertEqual(len(self.client.calls), 3)

    def test_pagination_bound_no_partial_output(self):
        self.client.pages = [{'workflow_runs': [run(1)], 'total_count': 2}]
        with self.assertRaises(GVError) as cm: self.call('--max-pages', '1', '--limit', '1')
        self.assertEqual(cm.exception.code, 'pagination_limit')

    def test_invalid_counts_overshoot_and_early_empty(self):
        for pages in ([{'workflow_runs': [run()], 'total_count': True}],
                      [{'workflow_runs': [run()], 'total_count': 0}],
                      [{'workflow_runs': [], 'total_count': 2}],
                      [{'workflow_runs': [run()], 'total_count': 3}, {'workflow_runs': [run(2)], 'total_count': 2}]):
            self.client.pages = pages
            with self.assertRaises(GVError) as cm: self.call()
            self.assertEqual(cm.exception.code, 'pagination_error')

    def test_duplicate_run_ids_and_bad_shapes(self):
        for pages in ([{'workflow_runs': [run(), run()], 'total_count': 2}],
                      [{'workflow_runs': [run()]}, {'workflow_runs': [run()]}],
                      [{'workflow_runs': [{'id': True}], 'total_count': 1}],
                      [{'runs': []}], [[]]):
            self.client.pages = pages
            with self.assertRaises(GVError): self.call()

    def test_invalid_input_before_network(self):
        for flags in (['--commit', 'abc'], ['--event', ''], ['--branch', '../x'], ['--branch', 'refs/tags/v1'],
                      ['--user', 'a/b'], ['--status', 'x\ny'], ['-w', '123'], ['-w', '../ci.yml']):
            with self.assertRaises(GVError): self.call(*flags)
        self.assertEqual(self.client.calls, [])

    def test_dry_run_leaves_at_me_and_workflow_unresolved(self):
        self.client.dry_run = True
        result = self.call('--user', '@me', '--workflow', 'Build')
        self.assertEqual(result['filters']['user'], '@me')
        self.assertTrue(result['dry_run']); self.assertEqual(self.client.calls, [])

    def test_cli_dispatch_public_guard(self):
        with patch('gitverse_cli.cli.Client', return_value=self.client):
            self.assertEqual(execute(parser().parse_args(['run', 'list', '-R', 'me/repo']))['total_matches'], 1)
            self.client.backend = 'web'
            with self.assertRaises(GVError): execute(parser().parse_args(['run', 'list', '-R', 'me/repo', '--backend', 'web']))
        self.assertTrue(all(c[0] == 'GET' for c in self.client.calls))
