"""Explicit PR title behavior, using synthetic public transport only."""
import copy
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from test_metadata import Public, REPO
import test_pr_create as core
import test_pr_create_fill as fill


class TitleTests(unittest.TestCase):
    setUp = core.CreateTests.setUp
    intercept = core.CreateTests.intercept

    def call(self, *flags):
        args = parser().parse_args(['pr', 'create', '-R', REPO, '--head', 'feature', '--base', 'main', *flags])
        with patch('gitverse_cli.cli.Client', return_value=self.c):
            return execute(args)

    def test_both_spellings_preserve_literal_unicode_whitespace_and_shell_text(self):
        title = '  Исправить 😀 e\u0301 "quote" $HOME `literal` #123  '
        for flag in ('--title', '-t'):
            with self.subTest(flag=flag):
                self.c = Public()
                result = self.call(flag, title, '--body', '')
                self.assertEqual(self.c.writes()[0][3]['title'], title)
                self.assertEqual(result['title'], title)
                self.assertTrue(result['create_verification']['readback'])
                self.assertEqual(self.c.calls[-1][1], 'repos/owner/repo/pulls/8')
                self.assertEqual(len(self.c.writes()), 1)

    def test_missing_blank_invalid_multiline_and_oversized_refuse_before_input_or_http(self):
        for title in (None, '', ' \t ', 'a\nb', 'a\rb', '\0', '\ud800', '😀'*262145):
            with self.subTest(title_kind=type(title).__name__, size=len(title or '')):
                self.c = Public()
                with patch('gitverse_cli.pr_create.read_value', side_effect=AssertionError('body input')):
                    with self.assertRaises(GVError) as caught:
                        self.call(*([] if title is None else ['--title', title]), '--body-file', '-')
                self.assertEqual(caught.exception.code, 'usage_error')
                self.assertEqual(self.c.calls, [])

    def test_utf8_byte_boundary_and_draft_normalization_bound(self):
        self.assertEqual(self.call('-t', '😀'*262144)['title'], '😀'*262144)
        self.c = Public()
        with self.assertRaises(GVError) as caught:
            self.call('-t', 'x'*1048576, '--draft')
        self.assertEqual(caught.exception.code, 'usage_error')
        self.assertEqual(self.c.calls, [])

    def test_draft_prefix_once_with_body_and_public_metadata(self):
        for title, expected in [('Literal 😀', 'Draft: Literal 😀'), ('dRaFt: Literal 😀', 'dRaFt: Literal 😀')]:
            self.c = Public()
            result = self.call('--title', title, '--draft', '--body', 'body\r\n', '-a', '@me', '-l', 'one')
            self.assertEqual((result['title'], result['body'], result['draft']), (expected, 'body\r\n', True))
            self.assertEqual([a['id'] for a in result['assignees']], [1])
            self.assertEqual([a['id'] for a in result['labels']], [11])
            self.assertEqual(len(self.c.writes()), 1)

    def test_editor_can_replace_explicit_title_seed(self):
        with patch('gitverse_cli.comment_editor.edit_text', return_value='Edited 😀\nBody\n') as editor:
            result = self.call('--title', 'Seed title', '--body', 'Seed body', '--editor')
        self.assertEqual(editor.call_args.args[0], 'Seed title\nSeed body')
        self.assertEqual((result['title'], result['body']), ('Edited 😀', 'Body\n'))

    def test_offline_exact_title_and_unresolved_metadata_no_auth_http_or_editor(self):
        for flags in ([], ['--assignee', '@me', '--label', 'one'], ['--editor']):
            with patch('gitverse_cli.client.load_auth', side_effect=AssertionError('auth')), patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('HTTP')), patch('gitverse_cli.comment_editor.edit_text', side_effect=AssertionError('editor')), patch('builtins.input', side_effect=AssertionError('prompt')):
                self.c = Client(dry_run=True)
                row = self.call('--title', 'Literal 😀', '--dry-run', *flags)
            request = row.get('base_request_template', row)
            self.assertEqual(row['network_requests'], 0)
            self.assertFalse(row['mutation_attempted'])
            self.assertEqual(row['mutation_outcome'], 'none')
            self.assertFalse(row['validation']['remote_eligibility'])
            if '--editor' in flags:
                self.assertEqual(row['editor']['seed']['title'], 'Literal 😀')
                self.assertNotIn('title', request['body'])
                self.assertFalse(row['validation']['final_request_known'])
            else:
                self.assertEqual(request['body']['title'], 'Literal 😀')
            if '--assignee' in flags:
                self.assertFalse(row['validation']['metadata_resolved'])
                self.assertFalse(row['validation']['final_request_known'])

    def test_wrong_identity_scope_and_permissions_prevent_title_write(self):
        for path, value in [('user', {'id': True, 'login': 'me'}), ('repos/'+REPO, {'id': 7, 'full_name': 'other/repo', 'private': True})]:
            self.c = Public()
            self.intercept(lambda m,p,r,kw: value if p == path else r)
            with self.assertRaises(GVError) as caught:
                self.call('-t', 'Title')
            self.assertFalse(caught.exception.details['mutation_attempted'])
            self.assertEqual(self.c.writes(), [])
        for status in (401, 403, 404):
            self.c = Public()
            self.c.request = lambda *a,**kw: (_ for _ in ()).throw(GVError('Denied', 'http_error', status))
            with self.assertRaises(GVError) as caught:
                self.call('-t', 'Title')
            self.assertEqual(caught.exception.details['mutation_outcome'], 'none')
            self.assertEqual(self.c.writes(), [])

    def test_malformed_or_different_title_in_post_or_readback_retains_ids(self):
        for stage in ('POST', 'readback'):
            for title in (None, False, 1, [], {}, 'Changed', ' Title '):
                self.c = Public()
                def hook(m,p,r,kw):
                    return {**r, 'title': copy.deepcopy(title)} if (m == 'POST' if stage == 'POST' else p.endswith('/pulls/8')) else r
                self.intercept(hook)
                with self.assertRaises(GVError) as caught:
                    self.call('--title', 'Title')
                self.assertEqual(caught.exception.code, 'verification_failed')
                self.assertEqual((caught.exception.details['pull_id'], caught.exception.details['pull_number']), (88, 8))
                self.assertEqual(caught.exception.details['mutation_outcome'], 'confirmed')
                self.assertEqual(len(self.c.writes()), 1)

    def test_uncertain_title_post_is_never_retried(self):
        def hook(m,p,r,kw):
            if m == 'POST':
                raise GVError('Unknown response', 'network_error')
            return r
        self.intercept(hook)
        with self.assertRaises(GVError) as caught:
            self.call('--title', 'Literal')
        details = caught.exception.details
        self.assertEqual(details['mutation_outcome'], 'unknown')
        self.assertIsNone(details['pull_id'])
        self.assertEqual((details['repository'], details['head'], details['base']), (REPO, 'feature', 'main'))
        self.assertEqual(len(self.c.writes()), 1)

    def test_same_title_creation_is_never_treated_as_noop(self):
        self.call('-t', 'Title')
        self.call('-t', 'Title')
        self.assertEqual(len(self.c.writes()), 2)

    def test_title_discovery_documents_precedence_and_adaptation(self):
        row = execute(parser().parse_args(['agent-info']))['pr_create_title']
        self.assertEqual(row['flags'], ['--title', '-t'])
        self.assertIn('fill', row['behavior'])
        self.assertIn('Draft:', row['adaptation'])


class TitleFillTests(unittest.TestCase):
    setUp = fill.CreateFillTests.setUp
    git = fill.CreateFillTests.git
    commit = fill.CreateFillTests.commit
    reset_client = fill.CreateFillTests.reset_client

    def test_explicit_title_overrides_each_fill_mode_and_editor_receives_it(self):
        for mode in ('--fill', '--fill-first', '--fill-verbose'):
            for editor in (False, True):
                self.reset_client()
                args = parser().parse_args(['pr','create','-R',REPO,'--head','feature','--base','main',mode,'--title','Explicit 😀',*(['--editor'] if editor else [])])
                with patch('gitverse_cli.cli.Client', return_value=self.c), patch('gitverse_cli.comment_editor.edit_text', return_value='Edited\nBody') as edit:
                    row = execute(args)
                self.assertEqual(row['title'], 'Edited' if editor else 'Explicit 😀')
                if editor:
                    self.assertTrue(edit.call_args.args[0].startswith('Explicit 😀\n'))
                else:
                    self.assertEqual(row['body'], 'Body\n\nLiteral $HOME\n')
                self.assertEqual(len(self.c.writes()), 1)
