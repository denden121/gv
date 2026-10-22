"""Offline PR preview contracts; network observations belong in live evidence."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError


class DryRunTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'GV_SETTINGS': '/nonexistent/gv-preview-settings'}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        for target in ('urllib.request.OpenerDirector.open', 'socket.socket.connect',
                       'builtins.input', 'gitverse_cli.client.load_auth',
                       'gitverse_cli.metadata.web_client'):
            guard = patch(target, side_effect=AssertionError(target))
            guard.start(); self.addCleanup(guard.stop)

    def preview(self, *flags, prefix=()):
        args = parser().parse_args([*prefix, 'pr', 'create', '-R', 'owner/repo',
                                   '--head', 'feature', '--base', 'main',
                                   '--title', 'Preview', *flags])
        with patch('subprocess.run', side_effect=AssertionError('process')):
            return execute(args)

    def test_plain_details_and_explicit_nonmutation_contract(self):
        row = self.preview('--dry-run', '--body', '  Текст 😀\r\n')
        self.assertEqual(row['body'], {'title': 'Preview', 'body': '  Текст 😀\r\n',
                                      'head': 'feature', 'base': 'main', 'draft': False})
        self.assertEqual(row['method'], 'POST')
        self.assertEqual(row['url'], 'https://api.gitverse.ru/repos/owner/repo/pulls')
        self.assertIs(row['dry_run'], True)
        self.assertIs(row['mutation_attempted'], False)
        self.assertEqual(row['mutation_outcome'], 'none')
        self.assertEqual(row['network_requests'], 0)
        self.assertIs(row['git_push'], False)
        self.assertEqual(row['validation'], {'local_inputs': True, 'remote_eligibility': False,
                                             'metadata_resolved': True, 'final_request_known': True})
        for key in ('id', 'number', 'create_verification'):
            self.assertNotIn(key, row)

    def test_metadata_is_unresolved_not_fabricated_wire_ids(self):
        row = self.preview('--dry-run', '-a', '@me', '-l', 'bug,help wanted',
                           '-r', 'team:42', '-r', 'reviewer')
        self.assertEqual(row['unresolved_metadata'], {'assignees': ['@me'],
            'labels': ['bug', 'help wanted'], 'reviewers': ['team:42', 'reviewer']})
        self.assertEqual(set(row['base_request_template']['body']), {'title', 'head', 'base', 'draft'})
        self.assertIs(row['validation']['metadata_resolved'], False)
        self.assertIs(row['validation']['final_request_known'], False)

    def test_credentials_and_cookie_files_not_loaded_or_exposed(self):
        with patch.dict(os.environ, {'GITVERSE_TOKEN': 'synthetic-secret',
                                    'GITVERSE_COOKIE_FILE': '/nonexistent/cookies',
                                    'GITVERSE_CSRF_TOKEN': 'synthetic-csrf',
                                    'GV_CONFIG': '/nonexistent/auth'}):
            row = self.preview('--dry-run', '-r', 'someone')
        self.assertNotIn('synthetic-secret', json.dumps(row))
        self.assertNotIn('synthetic-csrf', json.dumps(row))

    def test_flag_at_root_group_and_leaf_preserves_behavior(self):
        for prefix, tail in [(('--dry-run',), ()), (('pr',), ())]:
            if prefix == ('pr',):
                args = parser().parse_args(['pr', '--dry-run', 'create', '-R', 'owner/repo',
                    '--head', 'feature', '--base', 'main', '--title', 'Preview'])
                self.assertTrue(execute(args)['dry_run'])
            else:
                self.assertTrue(self.preview(*tail, prefix=prefix)['dry_run'])
        self.assertTrue(self.preview('--dry-run')['dry_run'])

    def test_repeated_preview_is_equal_and_not_a_creation_noop_claim(self):
        self.assertEqual(self.preview('--dry-run'), self.preview('--dry-run'))
        self.assertNotIn('changed', self.preview('--dry-run'))

    def test_body_file_stdin_and_draft_combination(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d)/'body.txt'; f.write_bytes(b' \r\n')
            row = self.preview('--dry-run', '-F', str(f), '-d')
        self.assertEqual(row['body']['title'], 'Draft: Preview')
        self.assertEqual(row['body']['body'], ' \r\n')
        with patch('sys.stdin', io.StringIO('stdin\n')):
            self.assertEqual(self.preview('--dry-run', '-F', '-')['body']['body'], 'stdin\n')

    def test_attachment_plan_does_not_invent_final_body(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d)/'image.png'; f.write_bytes(b'synthetic image bytes')
            row = self.preview('--dry-run', '--attach', str(f), '-a', '@me')
        self.assertIn('creation_plan', row)
        self.assertIs(row['creation_plan_body_is_original'], True)
        self.assertIs(row['validation']['final_request_known'], False)
        self.assertIs(row['mutation_attempted'], False)

    def test_invalid_local_values_refuse_before_any_request(self):
        for flags in [('--title', ''), ('--body', '\0'), ('--body', 'x'*1048577),
                      ('--head', 'main'), ('--base', '../bad'), ('-a', 'bad/name'),
                      ('-r', 'team:0'), ('-l', 'bug,')]:
            with self.subTest(flags=flags[:1]), self.assertRaises(GVError) as e:
                self.preview('--dry-run', *flags)
            self.assertEqual(e.exception.code, 'usage_error')

    def test_missing_base_refuses_instead_of_network_default_lookup(self):
        args = parser().parse_args(['pr','create','-R','owner/repo','--head','feature',
                                  '--title','Preview','--dry-run'])
        with patch('gitverse_cli.pr_create.configured_base', return_value=None), self.assertRaises(GVError) as e:
            execute(args)
        self.assertEqual(e.exception.code, 'usage_error')

    def test_configured_base_and_current_head_only_read_local_context(self):
        args = parser().parse_args(['pr','create','-R','owner/repo','--title','Preview','--dry-run'])
        with patch('gitverse_cli.pr_create_fill.git_read', return_value='current\n') as git, \
             patch('gitverse_cli.pr_create.configured_base', return_value='release/next'):
            row = execute(args)
        self.assertEqual(git.call_args.args[0], ['symbolic-ref', '--quiet', '--short', 'HEAD'])
        self.assertIsInstance(git.call_args.args[1], float)
        self.assertEqual(row['body']['head'], 'current')
        self.assertEqual(row['body']['base'], 'release/next')

    def test_expired_preview_budget_refuses(self):
        with patch('gitverse_cli.pr_create.time.monotonic', side_effect=[0, 61]), self.assertRaises(GVError) as e:
            self.preview('--dry-run')
        self.assertEqual(e.exception.code, 'deadline_exceeded')

    def test_web_backend_refuses_even_offline(self):
        with self.assertRaises(GVError) as e:
            self.preview('--dry-run', '--backend', 'web')
        self.assertEqual(e.exception.code, 'backend_unsupported')

    def test_discovery_states_offline_boundary(self):
        row = execute(parser().parse_args(['agent-info']))['pr_create_dry_run']
        self.assertEqual(row['flag'], '--dry-run')
        self.assertIn('metadata stays unresolved', row['limits'])
