import copy
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from gitverse_cli.pr_close import close_pull


class Fake:
    backend = 'public'; dry_run = False; base = 'https://api.gitverse.ru'
    def __init__(self):
        self.calls = []; self.reads = 0; self.hook = None; self.fail = None; self.ignore = False
        self.pull = {'id': 7, 'number': 9, 'state': 'open', 'merged': False, 'updated_at': 'v1',
                     'title': 'fixture', 'body': '', 'draft': False, 'changed_files': 1,
                     'head': {'sha': 'a' * 40, 'ref': 'topic', 'repo_id': 2},
                     'base': {'sha': 'b' * 40, 'ref': 'main', 'repo_id': 2,
                              'repo': {'id': 2, 'full_name': 'o/r'}}}
        self.comment = {'id': 55, 'user': {'id': 3}, 'body': '', 'updated_at': 'v1',
                        'issue_url': self.base + '/repos/o/r/issues/9'}
        self.created = {'id': 55}
    def request(self, method, path, query=None, body=None, **kw):
        self.calls.append((method, path, copy.deepcopy(body), kw))
        if self.fail: self.fail(method, path)
        if method == 'GET':
            if path == 'user': return {'id': 3, 'login': 'me'}
            if '/issues/comments/' in path: return copy.deepcopy(self.comment)
            self.reads += 1
            if self.hook: self.hook(self)
            return copy.deepcopy(self.pull)
        if method == 'POST':
            self.comment['body'] = body['body']
            self.pull['updated_at'] = 'v2'
            return copy.deepcopy(self.created)
        if not self.ignore: self.pull.update(state='closed', updated_at='v3')
        return copy.deepcopy(self.pull)


class CloseTests(unittest.TestCase):
    def setUp(self): self.c = Fake()
    def call(self, *flags):
        args = parser().parse_args(['pr', 'close', '9', '-R', 'o/r', *flags]); args.number = 9
        return close_pull(self.c, 'o/r', args)
    def writes(self): return [c for c in self.c.calls if c[0] != 'GET']
    def error(self, code, *flags):
        with self.assertRaises(GVError) as cm: self.call(*flags)
        self.assertEqual(cm.exception.code, code)
        return cm.exception
    def test_close_positive_preserves_pr_fields(self):
        r = self.call(); self.assertEqual(r['number'], 9); self.assertEqual(r['state'], 'closed')
        self.assertTrue(r['changed']); self.assertEqual(r['completed_steps'], ['close'])
        self.assertEqual([x[:3] for x in self.writes()], [('PATCH', 'repos/o/r/pulls/9', {'state': 'closed'})])
        self.assertEqual(len({c[3]['deadline'] for c in self.c.calls}), 1)
        self.assertTrue(all(c[3]['require_complete'] and c[3]['max_response_bytes'] == 1048576 for c in self.c.calls))
    def test_comment_then_close_exact_text_independent_read(self):
        r = self.call('-c', 'Текст\n$HOME\r\n')
        self.assertEqual(r['closing_comment_id'], 55); self.assertEqual(r['completed_steps'], ['comment', 'close'])
        self.assertEqual(self.writes()[0][2], {'body': 'Текст\n$HOME\r\n'})
        self.assertEqual([x[0] for x in self.writes()], ['POST', 'PATCH'])
        self.assertIn('repos/o/r/issues/comments/55', [x[1] for x in self.c.calls])
    def test_closed_is_noop_including_comment(self):
        self.c.pull['state'] = 'closed'
        self.assertFalse(self.call('-c', 'do not duplicate')['changed']); self.assertEqual(self.writes(), [])
    def test_merged_refuses(self):
        self.c.pull.update(state='closed', merged=True); self.error('pr_merged'); self.assertEqual(self.writes(), [])
    def test_malformed_identity_state_or_scope_no_write(self):
        for transform in (lambda p: p.update(id=True), lambda p: p.update(number=8),
                          lambda p: p.update(state='unknown'), lambda p: p.update(merged=None),
                          lambda p: p.update(updated_at=None), lambda p: p['base']['repo'].update(full_name='other/r'),
                          lambda p: p['base']['repo'].update(id=True), lambda p: p['head'].update(sha='x')):
            with self.subTest(transform=transform):
                self.c = Fake(); transform(self.c.pull); self.error('unexpected_response'); self.assertEqual(self.writes(), [])
    def test_concurrent_revision_no_write(self):
        def change(c):
            if c.reads == 2: c.pull['head']['sha'] = 'c' * 40
        self.c.hook = change; self.error('concurrent_change', '-c', 'text'); self.assertEqual(self.writes(), [])
    def test_comment_then_concurrent_change_retains_id_no_close(self):
        def change(c):
            if c.reads == 3: c.pull['state'] = 'closed'
        self.c.hook = change
        e = self.error('concurrent_change', '-c', 'text')
        self.assertEqual(e.details['closing_comment_id'], 55); self.assertEqual(e.details['completed_steps'], ['comment'])
        self.assertEqual(len(self.writes()), 1)
    def test_comment_wrong_author_parent_body_no_close(self):
        for field, value in [('user', {'id': 4}), ('issue_url', 'https://evil.example/repos/o/r/issues/9'), ('updated_at', None)]:
            with self.subTest(field=field):
                self.c = Fake(); self.c.comment[field] = value
                with self.assertRaises(GVError) as cm: self.call('-c', 'text')
                self.assertEqual(cm.exception.details['closing_comment_id'], 55)
                self.assertEqual(len(self.writes()), 1)
                self.assertFalse(any('evil.example' in c[1] for c in self.c.calls))
    def test_malformed_create_dto_does_not_retry(self):
        for dto in (None, [], {}, {'id': True}, {'id': -1}):
            self.c = Fake(); self.c.created = dto
            e = self.error('unexpected_response', '-c', 'text')
            self.assertTrue(e.details['mutation_attempted']); self.assertIsNone(e.details['closing_comment_id'])
            self.assertEqual(len(self.writes()), 1)
    def test_permissions_auth_and_timeout_keep_recovery(self):
        for method in ('POST', 'PATCH'):
            for status in (401, 403, 422, 500):
                self.c = Fake()
                def fail(m, p):
                    if m == method: raise GVError('denied', 'http_error', status)
                self.c.fail = fail
                e = self.error('http_error', '-c', 'text')
                self.assertEqual(e.status, status); self.assertEqual(e.details['stage'], 'comment' if method == 'POST' else 'close')
                self.assertEqual(len(self.writes()), 1 if method == 'POST' else 2)
                if method == 'PATCH': self.assertEqual(e.details['closing_comment_id'], 55)
    def test_unknown_patch_outcome_and_unconfirmed_close(self):
        def fail(m, p):
            if m == 'PATCH': raise GVError('timeout', 'network_error')
        self.c.fail = fail
        e = self.error('network_error'); self.assertTrue(e.details['mutation_attempted']); self.assertEqual(len(self.writes()), 1)
        self.c = Fake(); self.c.ignore = True
        self.assertEqual(self.error('verification_failed').details['stage'], 'verify_close')
    def test_comment_read_size_deadline_failure_preserves_id(self):
        for code in ('deadline_exceeded', 'response_limit', 'incomplete_response'):
            self.c = Fake()
            def fail(m, p):
                if '/issues/comments/' in p: raise GVError('bound', code)
            self.c.fail = fail
            e = self.error(code, '-c', 'text'); self.assertEqual(e.details['closing_comment_id'], 55)
            self.assertEqual(len(self.writes()), 1)
    def test_invalid_comment_offline(self):
        for text in ('', ' ', '\0', 'я' * 524289):
            self.c = Fake(); self.error('usage_error', '-c', text); self.assertEqual(self.c.calls, [])
    def test_web_refuses_before_network(self):
        self.c.backend = 'web'; self.error('backend_unsupported', '-c', 'text'); self.assertEqual(self.c.calls, [])
    def test_offline_dry_run_no_requests_or_auth(self):
        args = parser().parse_args(['pr', 'close', '9', '-R', 'o/r', '-c', 'text', '--dry-run'])
        with patch.object(Client, 'request', side_effect=AssertionError('network')):
            r = execute(args)
        self.assertTrue(r['dry_run']); self.assertEqual([x['method'] for x in r['requests']], ['POST', 'PATCH'])
    def test_branch_deletion_not_silently_accepted(self):
        from contextlib import redirect_stderr
        from io import StringIO
        with redirect_stderr(StringIO()), self.assertRaises(GVError):
            parser().parse_args(['pr', 'close', '9', '--delete-branch'])
