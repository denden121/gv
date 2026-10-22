import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.comment_edit import edit_comment


class Fake:
    backend = 'public'; base = 'https://api.gitverse.ru'; dry_run = False
    def __init__(self):
        self.calls = []; self.reads = 0; self.ignore = False; self.fail = None; self.hook = None
        self.parent = {'id': 7, 'number': 9, 'title': 'fixture', 'state': 'closed'}
        self.row = {'id': 55, 'user': {'id': 3}, 'body': 'old', 'updated_at': 'v1', 'issue_url': self.base + '/repos/o/r/issues/9'}
    def request(self, method, path, query=None, body=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(body), kwargs))
        if method == 'GET':
            if path == 'user': return {'id': 3, 'login': 'me'}
            if '/issues/comments/' in path:
                self.reads += 1
                if self.hook: self.hook(self)
                return copy.deepcopy(self.row)
            return copy.deepcopy(self.parent)
        if self.fail: raise self.fail
        if not self.ignore: self.row.update(body=body['body'], updated_at='v2')
        return None  # PATCH has no documented response DTO; GET is authoritative.


class CommentEditTests(unittest.TestCase):
    def setUp(self): self.client = Fake()
    def call(self, *flags, group='pr'):
        args = parser().parse_args([group, 'comment-edit', '9', '--comment-id', '55', *flags])
        args.number = 9
        return edit_comment(self.client, 'o/r', args)
    def writes(self): return [c for c in self.client.calls if c[0] != 'GET']

    def test_edit_closed_pr_discussion_patch_get_exact_text(self):
        result = self.call('--body', 'Новый\n$HOME $(literal)\r\n')
        self.assertTrue(result['changed']); self.assertEqual(result['comment_id'], 55)
        self.assertEqual(self.writes()[0][:3], ('PATCH', 'repos/o/r/issues/9/comments/55', {'body': result['body']}))
        self.assertTrue(self.client.calls[0][3]['require_complete'])
        self.assertEqual(len({c[3]['deadline'] for c in self.client.calls}), 1)

    def test_equal_body_noop(self):
        self.assertFalse(self.call('--body', 'old')['changed'])
        self.assertEqual(self.writes(), [])

    def test_issue_parent_accepts_pure_issue_refuses_pr(self):
        self.assertTrue(self.call('--body', 'new', group='issue')['changed'])
        self.client.parent['pull_request'] = {}
        with self.assertRaises(GVError) as cm: self.call('--body', 'other', group='issue')
        self.assertEqual(cm.exception.code, 'issue_is_pull_request')

    def test_parent_reference_scope_mismatch_never_followed(self):
        for url in ('https://evil.example/repos/o/r/issues/9', 'https://api.gitverse.ru/repos/o/other/issues/9',
                    'https://api.gitverse.ru/repos/o/r/issues/8', 'https://api.gitverse.ru/repos/o/r/issues/9?q=x',
                    'https://api.gitverse.ru/repos/o/r/issues/9#fragment', 'https://user@api.gitverse.ru/repos/o/r/issues/9'):
            self.client = Fake(); self.client.row['issue_url'] = url
            with self.assertRaises(GVError) as cm: self.call('--body', 'new')
            self.assertEqual(cm.exception.code, 'comment_parent_mismatch'); self.assertEqual(self.writes(), [])

    def test_other_author_refused_before_patch(self):
        self.client.row['user']['id'] = 4
        with self.assertRaises(GVError) as cm: self.call('--body', 'new')
        self.assertEqual(cm.exception.code, 'comment_not_owned'); self.assertEqual(self.writes(), [])

    def test_invalid_comment_body_version_id_author_refused(self):
        for key, value in (('id', True), ('user', {}), ('body', None), ('updated_at', None), ('issue_url', None)):
            self.client = Fake(); self.client.row[key] = value
            with self.assertRaises(GVError): self.call('--body', 'new')
            self.assertEqual(self.writes(), [])

    def test_preflight_body_or_version_race_refuses(self):
        for key, value in (('body', 'raced'), ('updated_at', 'v3')):
            self.client = Fake()
            def change(c):
                if c.reads == 2: c.row[key] = value
            self.client.hook = change
            with self.assertRaises(GVError) as cm: self.call('--body', 'new')
            self.assertEqual(cm.exception.code, 'concurrent_change')
            self.assertFalse(cm.exception.details['mutation_attempted']); self.assertEqual(self.writes(), [])

    def test_parent_replacement_refused(self):
        def change(c):
            if c.reads == 2: c.parent['id'] = 8
        self.client.hook = change
        with self.assertRaises(GVError) as cm: self.call('--body', 'new')
        self.assertEqual(cm.exception.code, 'concurrent_change'); self.assertEqual(self.writes(), [])

    def test_ignored_patch_is_not_success(self):
        self.client.ignore = True
        with self.assertRaises(GVError) as cm: self.call('--body', 'new')
        self.assertEqual(cm.exception.code, 'verification_failed')
        self.assertEqual(cm.exception.details['stage'], 'verify')
        self.assertTrue(cm.exception.details['mutation_attempted'])

    def test_network_patch_failure_no_retry_keeps_id_context(self):
        self.client.fail = GVError('timeout', 'network_error')
        with self.assertRaises(GVError) as cm: self.call('--body', 'new')
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(cm.exception.details['comment_id'], 55)
        self.assertEqual(cm.exception.details['stage'], 'edit')

    def test_after_patch_wrong_author_or_parent_fails(self):
        def change(c):
            if c.reads == 3: c.row['issue_url'] = c.base + '/repos/o/r/issues/10'
        self.client.hook = change
        with self.assertRaises(GVError) as cm: self.call('--body', 'new')
        self.assertEqual(cm.exception.details['stage'], 'verify')

    def test_body_file_exact_and_no_body_or_nul_oversize_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'comment.txt'; path.write_bytes('Привет\r\n'.encode())
            self.assertEqual(self.call('--body-file', str(path))['body'], 'Привет\r\n')
        for flags in ((), ('--body', ' '), ('--body', '\0'), ('--body', 'x' * 1048577)):
            self.client = Fake()
            with self.assertRaises(GVError): self.call(*flags)
            self.assertEqual(self.client.calls, [])

    def test_dry_run_no_user_or_comment_lookup(self):
        self.client.dry_run = True
        self.assertTrue(self.call('--body', 'new')['dry_run']); self.assertEqual(self.client.calls, [])

    def test_cli_pr_issue_dispatch_and_public_backend(self):
        with patch('gitverse_cli.cli.Client', return_value=self.client):
            self.assertTrue(execute(parser().parse_args(['pr', 'comment-edit', '9', '--comment-id', '55', '-R', 'o/r', '--body', 'new']))['changed'])
            self.assertTrue(execute(parser().parse_args(['issue', 'comment-edit', '9', '--comment-id', '55', '-R', 'o/r', '--body', 'next']))['changed'])
            self.client.backend = 'web'
            with self.assertRaises(GVError): execute(parser().parse_args(['pr', 'comment-edit', '9', '--comment-id', '55', '-R', 'o/r', '--body', 'other', '--backend', 'web']))
