"""Offline project planning/refusal only, never positive assignment evidence."""
import os
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from gitverse_cli.pr_create import create_pull
from gitverse_cli.pr_create_project import project_titles


class ProjectPlanTests(unittest.TestCase):
    def args(self, *flags):
        return parser().parse_args(['pr', 'create', '-R', 'owner/repo', '--head',
                                   'feature', '--base', 'main', '--title', 'Change', *flags])

    def preview(self, *flags):
        with patch('gitverse_cli.client.load_auth', side_effect=AssertionError('auth')):
            client = Client(dry_run=True)
            with patch.object(client.opener, 'open', side_effect=AssertionError('network')):
                return create_pull(client, 'owner/repo', self.args('--dry-run', *flags))

    def test_literal_title_and_alias(self):
        for name in ('Roadmap', '123', ' A Б ', 'Я' * 512):
            result = self.preview('-p', name)
            self.assertEqual(result['projects'], [{'title': name, 'id': None, 'resolved': False,
                                                  'assignment_supported': None, 'status': 'live_unimplemented'}])
            self.assertFalse(result['validation']['metadata_resolved'])
            self.assertFalse(result['validation']['final_request_known'])
            self.assertEqual(result['network_requests'], 0)
            self.assertEqual(result['mutation_outcome'], 'none')
            self.assertFalse(result['mutation_attempted'])
            self.assertNotIn('project_id', result['body'])
            self.assertNotIn('projects', result['body'])

    def test_repeated_csv_and_quoted_comma_preserved(self):
        result = self.preview('--project', 'One,Two', '-p', '"A,B",One', '-p', ' one ')
        self.assertEqual([p['title'] for p in result['projects']], ['One', 'Two', 'A,B', 'One', ' one '])

    def test_invalid_inputs_are_structured_errors_in_both_modes(self):
        for value in ('', ' ', ',', 'ok,', '"unterminated', '"ok"oops', '\ud800',
                      'x' * 1025, 'Я' * 513, 'x' * 1048577,
                      *('a' + chr(c) + 'b' for c in (*range(32), *range(127, 160)))):
            for dry in (True, False):
                with self.subTest(value=repr(value[:20]), dry=dry):
                    client = Client(dry_run=True)
                    client.dry_run = dry
                    with patch.object(client, 'request', side_effect=AssertionError('network')):
                        with self.assertRaises(GVError) as caught:
                            create_pull(client, 'owner/repo', self.args('-p', value))
                    self.assertEqual(caught.exception.code, 'usage_error')

    def test_count_bounds_include_repeats(self):
        self.assertEqual(len(project_titles([','.join(['x'] * 100)])), 100)
        for values in ([','.join(['x'] * 101)], ['x'] * 101):
            with self.assertRaises(GVError):
                project_titles(values)

    def test_nonstring_internal_values_fail_closed(self):
        for values in ('Roadmap', [], [None], [True], [1], [{}]):
            with self.assertRaises(GVError) as caught:
                project_titles(values)
            self.assertEqual(caught.exception.code, 'usage_error')

    def test_live_refuses_before_body_editor_or_network(self):
        client = Client(dry_run=True)
        client.dry_run = False
        with patch.object(client, 'request', side_effect=AssertionError('network')):
            with patch('gitverse_cli.comment_editor.edit_text', side_effect=AssertionError('editor')):
                with self.assertRaises(GVError) as caught:
                    create_pull(client, 'owner/repo', self.args('-p', 'Roadmap', '--editor', '--body-file', '/absent'))
        self.assertEqual(caught.exception.code, 'feature_unavailable')
        self.assertEqual(caught.exception.details, {'repository': 'owner/repo', 'stage': 'project_contract',
                                                   'mutation_attempted': False, 'mutation_outcome': 'none'})

    def test_metadata_draft_body_permission_combination(self):
        result = self.preview('-p', 'Roadmap', '-a', '@me', '-l', 'bug', '--draft',
                              '--body', '', '--no-maintainer-edit')
        self.assertEqual(result['unresolved_metadata']['assignees'], ['@me'])
        body = result['base_request_template']['body']
        self.assertEqual(body['body'], '')
        self.assertEqual(body['title'], 'Draft: Change')
        self.assertIs(body['maintainer_can_modify'], False)
        self.assertNotIn('project_id', body)

    def test_editor_preview_remains_unresolved(self):
        result = self.preview('-p', 'Roadmap', '--editor')
        self.assertFalse(result['editor']['launched'])
        self.assertFalse(result['validation']['final_request_known'])

    def test_milestone_and_project_plans_are_separate(self):
        result = self.preview('-p', 'Roadmap', '-m', 'Release')
        self.assertEqual(result['milestone']['name'], 'Release')
        self.assertEqual(result['projects'][0]['title'], 'Roadmap')

    def test_no_flag_preserves_known_request(self):
        result = self.preview()
        self.assertNotIn('projects', result)
        self.assertTrue(result['validation']['final_request_known'])

    def test_discovery_reports_incomplete_support(self):
        with patch.dict(os.environ, {}, clear=True):
            info = execute(parser().parse_args(['agent-info']))
        self.assertEqual(info['pr_create_project']['status'], 'offline_plan_only')
