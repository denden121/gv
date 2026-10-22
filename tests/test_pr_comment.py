import copy
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.pr_comment import discussion_comment


class Fake:
    backend = 'public'
    base = 'https://api.gitverse.ru'
    dry_run = False

    def __init__(self):
        self.parent = {'id': 7, 'number': 9, 'title': 'fixture', 'state': 'closed',
                       'merged': False, 'updated_at': 'v1', 'head': {'sha': 'a' * 40},
                       'base': {'sha': 'b' * 40, 'repo': {'id': 2, 'full_name': 'o/r'}}}
        self.rows = [self.row(55, 3), self.row(56, 4)]
        self.calls = []
        self.hook = None
        self.fail = None
        self.ignore = False

    def row(self, identity, author, stamp='2026-10-20T10:00:00Z'):
        return {'id': identity, 'user': {'id': author}, 'body': 'old', 'updated_at': 'v1',
                'created_at': stamp, 'issue_url': self.base + '/repos/o/r/issues/9'}

    def request(self, method, path, query=None, body=None, **kwargs):
        self.calls.append((method, path, query, body, kwargs))
        if self.hook: self.hook(self, method, path)
        if method != 'GET':
            if self.fail: raise self.fail
            if method == 'POST':
                row = self.row(99, 3)
                row['body'] = body['body']
                self.rows.append(row)
                return copy.deepcopy(row)
            if not self.ignore:
                next(r for r in self.rows if r['id'] == int(path.split('/')[-1])).update(body=body['body'], updated_at='v2')
            return None
        if path == 'user': return {'id': 3, 'login': 'me'}
        if path.endswith('/pulls/9'): return copy.deepcopy(self.parent)
        if path.endswith('/issues/9/comments'): return copy.deepcopy(self.rows)
        return copy.deepcopy(next(r for r in self.rows if r['id'] == int(path.split('/')[-1])))


class CommentTests(unittest.TestCase):
    def setUp(self): self.client = Fake()
    def call(self, *flags):
        args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', *flags])
        args.number = 9
        return discussion_comment(self.client, 'o/r', args)
    def writes(self): return [c for c in self.client.calls if c[0] != 'GET']

    def test_create_keeps_dto_and_independent_get_exact_text(self):
        result = self.call('--body', 'Привет\r\n$HOME $(literal)\n')
        self.assertEqual(result['id'], 99)
        self.assertEqual(self.writes()[0][3], {'body': result['body']})
        self.assertEqual(self.client.calls[-1][1], 'repos/o/r/issues/comments/99')
        self.assertTrue(all(c[4]['require_complete'] for c in self.client.calls))
        self.assertEqual(len({c[4]['deadline'] for c in self.client.calls}), 1)

    def test_unknown_outcome_one_write_preserves_identity(self):
        for flags in [()]:
            self.client = Fake()
            self.client.fail = GVError('timeout', 'network_error')
            with self.assertRaises(GVError) as caught: self.call('--body', 'new', *flags)
            self.assertEqual(len(self.writes()), 1)
            self.assertTrue(caught.exception.details['mutation_attempted'])
            self.assertEqual(caught.exception.details['number'], 9)
            if flags: self.assertEqual(caught.exception.details['comment_id'], 55)

    def test_create_bad_dto_keeps_known_id_without_retry(self):
        for bad in (None, {'id': True}, {'id': 99, 'user': {'id': 4}}):
            self.client = Fake()
            request = self.client.request
            def altered(method, path, query=None, body=None, **kwargs):
                result = request(method, path, query, body, **kwargs)
                return bad if method == 'POST' else result
            self.client.request = altered
            with self.assertRaises(GVError) as caught: self.call('--body', 'new')
            self.assertTrue(caught.exception.details['mutation_attempted'])
            self.assertEqual(len(self.writes()), 1)
            if bad and type(bad.get('id')) is int:
                self.assertEqual(caught.exception.details['comment_id'], 99)

    def test_create_readback_mismatch_retains_recovery_id(self):
        def hook(c, method, path):
            if method == 'GET' and path.endswith('/issues/comments/99'):
                c.rows[-1]['body'] = 'ignored or changed'
        self.client.hook = hook
        with self.assertRaises(GVError) as caught: self.call('--body', 'new')
        self.assertEqual(caught.exception.code, 'verification_failed')
        self.assertEqual(caught.exception.details['comment_id'], 99)
        self.assertEqual(caught.exception.details['stage'], 'verify')
        self.assertEqual(len(self.writes()), 1)

    def test_invalid_current_identity_refuses(self):
        request = self.client.request
        def altered(method, path, query=None, body=None, **kwargs):
            result = request(method, path, query, body, **kwargs)
            return {'id': True, 'login': 'me'} if path == 'user' else result
        self.client.request = altered
        with self.assertRaises(GVError): self.call('--body', 'new')
        self.assertEqual(self.writes(), [])

    def test_denied_preflight_no_write(self):
        for status in (401, 403, 404):
            self.client = Fake()
            def hook(c, method, path): raise GVError('denied', 'http_error', status)
            self.client.hook = hook
            with self.assertRaises(GVError): self.call('--body', 'new')
            self.assertEqual(self.writes(), [])

    def test_file_stdin_preserve_exact_utf8_and_preview_no_network(self):
        self.client.dry_run = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'body'
            path.write_bytes('Привет\r\n'.encode())
            self.assertEqual(self.call('--body-file', str(path))['body'], {'body': 'Привет\r\n'})
        with patch('sys.stdin', io.StringIO('stdin\r\n')):
            self.assertEqual(self.call('--body-file', '-')['body'], {'body': 'stdin\r\n'})
        self.assertEqual(self.client.calls, [])

    def test_invalid_input_and_nonregular_file_before_network(self):
        for text in ('', ' ', '\0', 'x' * 1048577):
            with self.assertRaises(GVError): self.call('--body', text)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'body'
            path.write_bytes(b'\xff')
            with self.assertRaises(GVError): self.call('--body-file', str(path))
            path.write_bytes(b'x' * 1048577)
            with self.assertRaises(GVError): self.call('--body-file', str(path))
            path.unlink(); path.symlink_to(directory)
            with self.assertRaises(GVError): self.call('--body-file', str(path))
        self.assertEqual(self.client.calls, [])

    def test_parent_repository_and_identity_malformed_refuse_before_write(self):
        for change in ({'number': 10}, {'id': True}, {'base': {'sha': 'b'*40, 'repo': {'id': 2, 'full_name': 'elsewhere/r'}}}, {'state': 'unknown'}, {'head': {}}):
            self.client = Fake(); self.client.parent.update(change)
            with self.assertRaises(GVError): self.call('--body', 'text')
            self.assertEqual(self.writes(), [])

    def test_concurrent_parent_refuses_before_post(self):
        count = 0
        def hook(c, method, path):
            nonlocal count
            if '/pulls/' in path:
                count += 1
                if count == 2: c.parent['head']['sha'] = 'c' * 40
        self.client.hook = hook
        with self.assertRaises(GVError) as caught: self.call('--body', 'text')
        self.assertEqual(caught.exception.code, 'concurrent_change')
        self.assertFalse(caught.exception.details['mutation_attempted'])
        self.assertEqual(self.writes(), [])

    def test_each_invocation_appends_even_equal_text(self):
        self.call('--body', 'same'); self.call('--body', 'same')
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual([c[0] for c in self.writes()], ['POST', 'POST'])

    def test_wrong_created_and_readback_author_parent_identity(self):
        for stage in ('POST', 'GET'):
            for key, value in [('user', {'id': 4}), ('issue_url', 'https://evil.example/repos/o/r/issues/9'), ('issue_url', self.client.base + '/repos/o/wrong/issues/9'), ('body', None), ('updated_at', None)]:
                self.client = Fake(); original = self.client.request
                def altered(method, path, query=None, body=None, **kwargs):
                    row = original(method, path, query, body, **kwargs)
                    if (method == 'POST' and stage == 'POST') or (method == 'GET' and '/issues/comments/' in path and stage == 'GET'):
                        row[key] = value
                    return row
                self.client.request = altered
                with self.assertRaises(GVError) as caught: self.call('--body', 'new')
                self.assertEqual(caught.exception.details['comment_id'], 99)
                self.assertEqual(len(self.writes()), 1)
                self.assertTrue(all('evil.example' not in c[1] for c in self.client.calls))

    def test_limits_preflight_and_after_post_preserve_recovery(self):
        for post in (False, True):
            for code in ('deadline_exceeded', 'response_limit', 'incomplete_response'):
                self.client = Fake()
                def hook(c, method, path):
                    if (post and '/issues/comments/' in path) or (not post and '/pulls/' in path):
                        raise GVError('bounded', code)
                self.client.hook = hook
                with self.assertRaises(GVError) as caught: self.call('--body', 'new')
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(self.writes()), int(post))
                if post: self.assertEqual(caught.exception.details['comment_id'], 99)
                self.assertTrue(all(c[4]['max_response_bytes'] == 33554432 for c in self.client.calls))

    def test_denied_post_does_not_retry(self):
        for status in (401, 403, 404, 422, 500):
            self.client = Fake(); self.client.fail = GVError('denied', 'http_error', status)
            with self.assertRaises(GVError) as caught: self.call('--body', 'new')
            self.assertEqual(caught.exception.status, status)
            self.assertTrue(caught.exception.details['mutation_attempted'])
            self.assertEqual(len(self.writes()), 1)

    def test_offline_execute_forbids_network_and_web_refuses(self):
        from gitverse_cli.client import Client
        args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', '--body', 'text', '--dry-run'])
        with patch.object(Client, 'request', side_effect=AssertionError('network')):
            self.assertTrue(execute(args)['dry_run'])
        self.client.backend = 'web'
        with patch('gitverse_cli.cli.Client', return_value=self.client), self.assertRaises(GVError): execute(args)
        self.assertEqual(self.client.calls, [])

    def test_commit_without_inline_refuses_offline(self):
        with self.assertRaises(GVError): self.call('--body', 'text', '--commit', 'abc')
        self.assertEqual(self.client.calls, [])
    def test_real_transport_post_redirect_refused_without_credential_forwarding(self):
        import json
        import urllib.error
        from unittest.mock import Mock
        from gitverse_cli.client import Client, NoRedirect
        class Response(io.BytesIO): status = 200
        c = Client(dry_run=True); c.dry_run = False
        c.headers['Authorization'] = 'Bearer synthetic-test-token'
        c.opener = Mock()
        c.opener.open.side_effect = [Response(json.dumps(d).encode()) for d in
                                    (self.client.parent, {'id': 3, 'login': 'me'}, self.client.parent)] + [
            urllib.error.HTTPError(c.base, 302, 'refused', {'Location': 'https://other.invalid/comment'}, None)]
        args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', '--body', 'text']); args.number = 9
        with self.assertRaises(GVError) as caught: discussion_comment(c, 'o/r', args)
        self.assertEqual(caught.exception.status, 302)
        self.assertTrue(caught.exception.details['mutation_attempted'])
        self.assertEqual(c.opener.open.call_count, 4)
        for call in c.opener.open.call_args_list:
            request = call.args[0]
            self.assertTrue(request.full_url.startswith(c.base + '/'))
            self.assertIsNone(request.get_header('Cookie'))
        self.assertTrue(any(isinstance(h, NoRedirect) for h in Client(dry_run=True).opener.handlers))
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.invalid/comment'))

    def test_real_transport_deadline_partial_and_size_fail_closed(self):
        from unittest.mock import Mock
        from gitverse_cli.client import Client
        class Response(io.BytesIO): status = 200
        for status, payload, code in ((206, b'{}', 'incomplete_response'), (200, b'x' * 33554433, 'response_limit')):
            c = Client(dry_run=True); c.dry_run = False; c.opener = Mock()
            response = Response(payload); response.status = status; c.opener.open.return_value = response
            args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', '--body', 'text']); args.number = 9
            with self.assertRaises(GVError) as caught: discussion_comment(c, 'o/r', args)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(c.opener.open.call_count, 1)
        c = Client(dry_run=True); c.dry_run = False; c.opener = Mock()
        with self.assertRaises(GVError) as caught: c.request('POST', 'repos/o/r/issues/9/comments', deadline=time.monotonic() - 1)
        self.assertEqual(caught.exception.code, 'deadline_exceeded'); c.opener.open.assert_not_called()
    def test_broken_transport_preflight_post_and_readback_structured_recovery(self):
        import http.client
        for stage in ('preflight', 'create', 'verify'):
            for failure in (http.client.IncompleteRead(b'private-partial-response'), ConnectionResetError('private transport details'), OSError('private transport details')):
                self.client = Fake()
                def hook(c, method, path):
                    if ((stage == 'preflight' and '/pulls/' in path) or
                        (stage == 'create' and method == 'POST') or
                        (stage == 'verify' and '/issues/comments/' in path)):
                        raise failure
                self.client.hook = hook
                with self.assertRaises(GVError) as caught: self.call('--body', 'text')
                error = caught.exception
                self.assertEqual(error.code, 'network_error')
                self.assertNotIn('private', str(error))
                self.assertEqual(error.details['repository'], 'o/r')
                self.assertEqual(error.details['number'], 9)
                self.assertEqual(error.details['mutation_attempted'], stage != 'preflight')
                self.assertEqual(len(self.writes()), int(stage != 'preflight'))
                if stage != 'preflight': self.assertEqual(error.details['stage'], stage)
                if stage == 'verify': self.assertEqual(error.details['comment_id'], 99)

    def test_real_transport_incomplete_post_stream_no_retry_or_raw_body(self):
        import http.client
        import json
        from unittest.mock import Mock
        from gitverse_cli.client import Client
        class Response(io.BytesIO): status = 200
        class Broken(Response):
            def read1(self, size): raise http.client.IncompleteRead(b'private-response')
        c = Client(dry_run=True); c.dry_run = False; c.opener = Mock()
        c.opener.open.side_effect = [Response(json.dumps(d).encode()) for d in
                                    (self.client.parent, {'id': 3, 'login': 'me'}, self.client.parent)] + [Broken(b'')]
        args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', '--body', 'text']); args.number = 9
        with self.assertRaises(GVError) as caught: discussion_comment(c, 'o/r', args)
        self.assertEqual(caught.exception.code, 'network_error')
        self.assertEqual(c.opener.open.call_count, 4)
        self.assertTrue(caught.exception.details['mutation_attempted'])
        self.assertEqual(caught.exception.details['stage'], 'create')
        self.assertNotIn('private-response', json.dumps(caught.exception.as_dict()))
