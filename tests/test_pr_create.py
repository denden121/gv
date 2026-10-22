import copy
import http.client
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from test_metadata import Public, REPO


def framed_response(row, status=200, extra_length=0, chunked=False, terminated=True):
    """Exercise the stdlib HTTP parser, without sockets or HTTP mocks."""
    data = json.dumps(row).encode()
    headers = (b'Transfer-Encoding: chunked\r\n' if chunked else
               b'Content-Length: ' + str(len(data) + extra_length).encode() + b'\r\n')
    payload = (('%x\r\n' % len(data)).encode() + data + b'\r\n' +
               (b'0\r\n\r\n' if terminated else b'')) if chunked else data
    class Socket:
        def makefile(self, *args, **kwargs):
            return io.BytesIO(b'HTTP/1.1 ' + str(status).encode() + b' OK\r\n' + headers + b'\r\n' + payload)
    response = http.client.HTTPResponse(Socket())
    response.begin()
    return response


class CreateTests(unittest.TestCase):
    def setUp(self):
        self.c = Public()
        self.env = patch.dict(os.environ, {}, clear=True); self.env.start(); self.addCleanup(self.env.stop)

    def create(self, *flags):
        args = parser().parse_args(['pr', 'create', '-R', REPO, '--head', 'feature', '--base', 'main', '--title', 'Created', *flags])
        with patch('gitverse_cli.cli.Client', return_value=self.c):
            return execute(args)

    def intercept(self, hook):
        original = self.c.request
        def request(method, path, query=None, body=None, **kwargs):
            row = original(method, path, query, body, **kwargs)
            return hook(method, path, row, kwargs)
        self.c.request = request

    def framed_create(self, truncated_stage=None, chunked=False, terminated=True):
        from unittest.mock import Mock
        from gitverse_cli.client import Client
        original = self.c.request
        transports = []
        def request(method, path, query=None, body=None, **kwargs):
            row = original(method, path, query, body, **kwargs)
            if method == 'POST' or path.endswith('/pulls/8'):
                transport = Client(dry_run=True)
                transport.dry_run = False
                transport.opener = Mock()
                stage = 'create' if method == 'POST' else 'verify_create'
                transport.opener.open.return_value = framed_response(
                    row, 201 if method == 'POST' else 200,
                    100 if truncated_stage == stage else 0, chunked, terminated)
                transports.append(transport)
                return transport.request(method, path, query, body, **kwargs)
            return row
        self.c.request = request
        return transports

    def test_actual_http_truncated_post_valid_json_refused_without_retry(self):
        transports = self.framed_create('create')
        with self.assertRaises(GVError) as caught:
            self.create()
        error = caught.exception
        self.assertEqual(error.code, 'incomplete_response')
        self.assertEqual(error.details['stage'], 'create')
        self.assertTrue(error.details['mutation_attempted'])
        self.assertIsNone(error.details['pull_id'])
        self.assertIsNone(error.details['pull_number'])
        self.assertEqual((error.details['repository'], error.details['head'], error.details['base']),
                         (REPO, 'feature', 'main'))
        self.assertEqual(len(self.c.writes()), 1)
        self.assertEqual(len(transports), 1)
        transports[0].opener.open.assert_called_once()

    def test_actual_http_truncated_readback_valid_json_preserves_ids(self):
        transports = self.framed_create('verify_create')
        with self.assertRaises(GVError) as caught:
            self.create()
        error = caught.exception
        self.assertEqual(error.code, 'incomplete_response')
        self.assertEqual(error.details['stage'], 'verify_create')
        self.assertTrue(error.details['mutation_attempted'])
        self.assertEqual((error.details['pull_id'], error.details['pull_number']), (88, 8))
        self.assertEqual(len(self.c.writes()), 1)
        self.assertEqual(len(transports), 2)
        for transport in transports:
            transport.opener.open.assert_called_once()

    def test_actual_http_complete_fixed_and_chunked_success(self):
        for chunked in (False, True):
            with self.subTest(chunked=chunked):
                self.c = Public()
                transports = self.framed_create(chunked=chunked)
                self.assertTrue(self.create()['create_verification']['readback'])
                self.assertEqual(len(self.c.writes()), 1)
                self.assertEqual(len(transports), 2)

    def test_actual_http_unterminated_chunked_post_never_retried(self):
        transports = self.framed_create(chunked=True, terminated=False)
        with self.assertRaises(GVError) as caught:
            self.create()
        self.assertEqual(caught.exception.code, 'network_error')
        self.assertTrue(caught.exception.details['mutation_attempted'])
        self.assertEqual(len(self.c.writes()), 1)
        transports[0].opener.open.assert_called_once()

    def test_actual_http_framing_checked_for_raw_and_multichunk_bodies(self):
        from unittest.mock import Mock
        from gitverse_cli.client import Client
        for raw in (False, True):
            for row in ({}, {'body': 'x' * 131072}):
                with self.subTest(raw=raw, size=len(json.dumps(row))):
                    c = Client(dry_run=True); c.dry_run = False
                    c.opener = Mock()
                    c.opener.open.return_value = framed_response(row, extra_length=1)
                    with self.assertRaises(GVError) as caught:
                        c.request('GET', 'path', require_complete=True,
                                  max_response_bytes=262144, raw=raw)
                    self.assertEqual(caught.exception.code, 'incomplete_response')
                    c.opener.open.assert_called_once()

    def test_actual_http_empty_eof_is_not_complete_declared_body(self):
        from unittest.mock import Mock
        from gitverse_cli.client import Client
        class Socket:
            def makefile(self, *args, **kwargs):
                return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n')
        response = http.client.HTTPResponse(Socket()); response.begin()
        c = Client(dry_run=True); c.dry_run = False; c.opener = Mock()
        c.opener.open.return_value = response
        with self.assertRaises(GVError) as caught:
            c.request('GET', 'path', require_complete=True, max_response_bytes=10)
        self.assertEqual(caught.exception.code, 'incomplete_response')
        c.opener.open.assert_called_once()

    def test_single_post_separate_readback_and_bounds(self):
        options = []
        self.intercept(lambda m, p, r, kw: options.append((m, kw)) or r)
        result = self.create('--body', 'Literal\n`$HOME`\r\n')
        self.assertTrue(result['create_verification']['readback'])
        self.assertEqual(len(self.c.writes()), 1)
        self.assertEqual(self.c.calls[-1][1], 'repos/owner/repo/pulls/8')
        self.assertEqual({v['deadline'] for _, v in options}, {options[0][1]['deadline']})
        self.assertTrue(all(v['require_complete'] and v['max_response_bytes'] == 33554432 for _, v in options))
        self.assertEqual([v['expected_status'] for m, v in options if m == 'POST'], [201])

    def test_file_and_stdin_exact_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'body'; p.write_bytes('Текст\r\n\n'.encode())
            self.assertEqual(self.create('--body-file', str(p))['body'], 'Текст\r\n\n')
        self.c = Public()
        with patch('sys.stdin', io.StringIO('stdin\n')):
            self.assertEqual(self.create('--body-file', '-')['body'], 'stdin\n')

    def test_invalid_inputs_never_network(self):
        for flags in [('--title', ''), ('--title', 'a\nb'), ('--body', 'x\0'), ('--head', 'other:bad..branch'), ('--head', 'main'), ('--base', '../bad'), ('--body', 'x'*1048577)]:
            with self.subTest(flags=flags[0]):
                with self.assertRaises(GVError): self.create(*flags)
                self.assertEqual(self.c.calls, [])

    def test_body_file_malformed_oversized_nonregular(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'body'
            for value in (b'\xff', b'\0', b'x'*1048577):
                p.write_bytes(value)
                with self.assertRaises(GVError): self.create('--body-file', str(p))
            with self.assertRaises(GVError): self.create('--body-file', tmp)
            self.assertEqual(self.c.calls, [])

    def test_wrong_repository_and_malformed_user_preflight_no_write(self):
        for target, replacement in [('repos/owner/repo', {'id': 7, 'full_name': 'other/repo', 'private': True}), ('user', {'id': True, 'login': 'me'})]:
            self.c = Public()
            self.intercept(lambda m, p, r, kw: replacement if p == target else r)
            with self.assertRaises(GVError) as caught: self.create()
            self.assertFalse(caught.exception.details['mutation_attempted'])
            self.assertEqual(self.c.writes(), [])

    def test_denied_preflight_no_write(self):
        for status in (401, 403, 404):
            self.c = Public()
            self.c.request = lambda *a, **kw: (_ for _ in ()).throw(GVError('denied', 'http_error', status))
            with self.assertRaises(GVError) as e: self.create()
            self.assertFalse(e.exception.details['mutation_attempted'])

    def test_unknown_post_never_retried(self):
        def hook(method, path, row, kw):
            if method == 'POST': raise GVError('unknown', 'network_error')
            return row
        self.intercept(hook)
        with self.assertRaises(GVError) as e: self.create()
        self.assertEqual(len(self.c.writes()), 1)
        self.assertTrue(e.exception.details['mutation_attempted'])
        self.assertIsNone(e.exception.details['pull_number'])
        self.assertEqual(e.exception.details['head'], 'feature')

    def test_malformed_post_retains_available_ids(self):
        for row in (None, {}, {'id': 88}, {'number': 8}, {'id': 88, 'number': 8, 'head': []}):
            self.c = Public(); self.intercept(lambda m, p, r, kw: row if m == 'POST' else r)
            with self.assertRaises(GVError) as e: self.create()
            self.assertEqual(e.exception.details['pull_id'], row.get('id') if isinstance(row, dict) else None)
            self.assertEqual(len(self.c.writes()), 1)

    def test_bad_created_fields_refuse(self):
        replacements = [('user', {'id': 2, 'login': 'alice'}), ('title', 'changed'), ('body', None), ('draft', 0), ('state', 'closed'), ('base', {'ref': 'main', 'sha': 'b'*40, 'repo': {'id': 7, 'full_name': []}})]
        for key, value in replacements:
            self.c = Public()
            self.intercept(lambda m, p, r, kw: {**r, key: value} if m == 'POST' else r)
            with self.assertRaises(GVError) as e: self.create()
            self.assertEqual(e.exception.details['pull_number'], 8)

    def test_readback_mismatch_and_concurrency_retain_ids(self):
        for key, value in [('id', 89), ('number', 9), ('title', 'changed'), ('head', {'ref': 'feature', 'sha': 'c'*40, 'repo': {'id': 7, 'full_name': REPO}})]:
            self.c = Public()
            self.intercept(lambda m, p, r, kw: {**r, key: value} if p.endswith('/pulls/8') else r)
            with self.assertRaises(GVError) as e: self.create()
            self.assertEqual(e.exception.details['pull_id'], 88)
            self.assertEqual(e.exception.details['stage'], 'verify_create')
            self.assertEqual(len(self.c.writes()), 1)

    def test_readback_limit_deadline_partial_denial_no_retry(self):
        for code in ('response_limit', 'deadline_exceeded', 'incomplete_response', 'http_error'):
            self.c = Public()
            def hook(m, p, r, kw):
                if p.endswith('/pulls/8'): raise GVError('failed', code)
                return r
            self.intercept(hook)
            with self.assertRaises(GVError) as e: self.create()
            self.assertEqual(e.exception.details['pull_number'], 8)
            self.assertEqual(len(self.c.writes()), 1)

    def test_dryrun_offline_no_credentials_or_existence_claim(self):
        from gitverse_cli.client import Client
        self.c = Client(dry_run=True)
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')), patch('builtins.input', side_effect=AssertionError('prompt')):
            result = self.create('--dry-run')
        self.assertEqual(result['method'], 'POST')
        self.assertNotIn('number', result)

    def test_repeat_create_is_not_noop_or_automatic_retry(self):
        self.create()
        def deny(m, p, r, kw):
            if m == 'POST': raise GVError('duplicate', 'http_error', 400)
            return r
        self.intercept(deny)
        with self.assertRaises(GVError): self.create()
        self.assertEqual(len(self.c.writes()), 2)

    def test_default_base_is_validated(self):
        args = parser().parse_args(['pr','create','-R',REPO,'--head','feature','--title','Created'])
        # This exercises the API fallback, independent of the runner's checkout
        # or effective Git config. Real checkout selection has flag-specific tests.
        with patch('gitverse_cli.cli.Client', return_value=self.c), patch('gitverse_cli.pr_create.configured_base', return_value=None):
            self.assertEqual(execute(args)['base']['ref'], 'main')

    def test_http_stream_interruption_becomes_structured_recovery(self):
        import http.client
        from gitverse_cli.pr_create import Bounded
        import time
        self.c.request = lambda *a, **kw: (_ for _ in ()).throw(http.client.IncompleteRead(b''))
        with self.assertRaises(GVError) as e: Bounded(self.c, time.monotonic()+5).request('POST','repos/owner/repo/pulls')
        self.assertEqual(e.exception.code,'network_error')

    def test_real_transport_redirect_never_forwards_credentials(self):
        import urllib.error
        from unittest.mock import Mock
        from gitverse_cli.client import Client
        from gitverse_cli.pr_create import Bounded
        import time
        with patch.dict(os.environ, {'GITVERSE_TOKEN':'synthetic-only-token'}):
            c=Client()
        c.opener=Mock()
        c.opener.open.side_effect=urllib.error.HTTPError('https://api.gitverse.ru/repos/owner/repo/pulls',302,'redirect',{'Location':'https://foreign.invalid/'},None)
        with self.assertRaises(GVError): Bounded(c,time.monotonic()+5).request('POST','repos/owner/repo/pulls',body={})
        self.assertEqual(c.opener.open.call_count,1)
        req=c.opener.open.call_args.args[0]
        self.assertEqual(req.full_url,'https://api.gitverse.ru/repos/owner/repo/pulls')
        self.assertEqual(req.get_header('Authorization'),'Bearer synthetic-only-token')
        self.assertIsNone(req.get_header('Cookie'))

    def test_real_transport_size_partial_wrong_status_and_deadline(self):
        import time
        from unittest.mock import Mock
        from gitverse_cli.client import Client
        from gitverse_cli.pr_create import Bounded
        for status, payload, code in [(206,b'{}','unexpected_response'),(200,b'{}','unexpected_response'),(201,b'not-json','unexpected_response'),(201,b'x'*33554433,'response_limit')]:
            c=Client(dry_run=True);c.dry_run=False
            response=io.BytesIO(payload);response.status=status
            c.opener=Mock();c.opener.open.return_value=response
            with self.assertRaises(GVError) as e: Bounded(c,time.monotonic()+5).request('POST','repos/owner/repo/pulls',body={})
            self.assertEqual(e.exception.code,code)
            self.assertEqual(c.opener.open.call_count,1)
        c=Client(dry_run=True);c.dry_run=False;c.opener=Mock()
        with self.assertRaises(GVError) as e: Bounded(c,time.monotonic()-1).request('POST','repos/owner/repo/pulls',body={})
        self.assertEqual(e.exception.code,'deadline_exceeded');c.opener.open.assert_not_called()
