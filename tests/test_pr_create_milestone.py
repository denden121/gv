"""Planning/refusal contracts only; no positive milestone assignment claim."""
import os
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from gitverse_cli.pr_create import create_pull


class MilestonePlanTests(unittest.TestCase):
    def args(self, *flags):
        return parser().parse_args(['pr', 'create', '-R', 'owner/repo', '--head',
                                   'feature', '--base', 'main', '--title', 'Change', *flags])

    def preview(self, *flags):
        args = self.args('--dry-run', *flags)
        with patch('gitverse_cli.client.load_auth', side_effect=AssertionError('auth')):
            client = Client(dry_run=True)
            with patch.object(client.opener, 'open', side_effect=AssertionError('network')):
                return create_pull(client, 'owner/repo', args)

    def test_literal_names_and_short_alias_remain_unresolved(self):
        for flag, name in [('--milestone', 'Release 1'), ('-m', '123'), ('-m', ' A,Б '),
                           ('-m', 'x' * 1024), ('-m', 'Я' * 512)]:
            with self.subTest(name=name[:20]):
                result = self.preview(flag, name)
                self.assertEqual(result['milestone']['name'], name)
                self.assertIsNone(result['milestone']['id'])
                self.assertIsNone(result['milestone']['assignment_supported'])
                self.assertFalse(result['validation']['metadata_resolved'])
                self.assertFalse(result['validation']['final_request_known'])
                self.assertEqual(result['network_requests'], 0)
                self.assertFalse(result['mutation_attempted'])
                self.assertEqual(result['mutation_outcome'], 'none')
                self.assertNotIn('milestone', result['body'])

    def test_invalid_names_refuse_before_network(self):
        for name in ['', ' ', '\t', 'a\nb', 'a\0b', 'a\x7fb', 'x'*1025, 'Я'*513, '\ud800']:
            for dry in (True, False):
                with self.subTest(name=repr(name[:8]), dry=dry):
                    client = Client(dry_run=True)
                    client.dry_run = dry
                    with patch.object(client, 'request', side_effect=AssertionError('network')):
                        with self.assertRaises(GVError) as caught:
                            create_pull(client, 'owner/repo', self.args('-m', name))
                    self.assertEqual(caught.exception.code, 'usage_error')

    def test_live_refuses_without_reading_body_or_launching_editor(self):
        client = Client(dry_run=True)
        client.dry_run = False
        args = self.args('-m', 'Release', '--editor', '--body-file', '/nonexistent')
        with patch.object(client, 'request', side_effect=AssertionError('network')):
            with patch('gitverse_cli.comment_editor.edit_text', side_effect=AssertionError('editor')):
                with self.assertRaises(GVError) as caught:
                    create_pull(client, 'owner/repo', args)
        self.assertEqual(caught.exception.code, 'feature_unavailable')
        self.assertEqual(caught.exception.details, {
            'repository': 'owner/repo', 'stage': 'milestone_contract',
            'mutation_attempted': False, 'mutation_outcome': 'none'})

    def test_unicode_c1_controls_refuse_in_both_modes(self):
        for codepoint in range(128, 160):
            for dry in (True, False):
                with self.subTest(codepoint=codepoint, dry=dry):
                    client = Client(dry_run=True)
                    client.dry_run = dry
                    with patch.object(client, 'request', side_effect=AssertionError('network')):
                        with self.assertRaises(GVError) as caught:
                            create_pull(client, 'owner/repo', self.args('-m', 'a' + chr(codepoint) + 'b'))
                    self.assertEqual(caught.exception.code, 'usage_error')

    def test_nonstring_internal_argument_is_a_structured_usage_error(self):
        for value in (123, True, [], {}):
            with self.subTest(value=value):
                args = self.args()
                args.milestone = value
                with self.assertRaises(GVError) as caught:
                    create_pull(Client(dry_run=True), 'owner/repo', args)
                self.assertEqual(caught.exception.code, 'usage_error')

    def test_metadata_draft_and_empty_body_preview(self):
        result = self.preview('-m', 'Release', '-a', '@me', '-l', 'bug', '--draft', '--body', '')
        self.assertEqual(result['unresolved_metadata']['assignees'], ['@me'])
        body = result['base_request_template']['body']
        self.assertEqual(body['title'], 'Draft: Change')
        self.assertEqual(body['body'], '')
        self.assertNotIn('milestone', body)

    def test_editor_preview_preserves_unknowns(self):
        result = self.preview('-m', 'Release', '--editor')
        self.assertFalse(result['editor']['launched'])
        self.assertFalse(result['validation']['final_request_known'])

    def test_no_flag_preserves_known_request(self):
        result = self.preview()
        self.assertNotIn('milestone', result)
        self.assertTrue(result['validation']['final_request_known'])

    def test_discovery_describes_partial_support(self):
        with patch.dict(os.environ, {}, clear=True):
            info = execute(parser().parse_args(['agent-info']))
        self.assertEqual(info['pr_create_milestone']['status'], 'offline_plan_only')
