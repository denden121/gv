"""Own-last edits without implicit creation, independent of fallback coverage."""
import copy
import http.client
import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError, NoRedirect
from gitverse_cli.pr_comment import discussion_comment
from test_pr_comment import Fake


class EditLastTests(unittest.TestCase):
    def setUp(self):
        self.client = Fake()

    def args(self, *flags):
        args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', '--edit-last', *flags])
        args.number = 9
        return args

    def call(self, text='new', *flags):
        return discussion_comment(self.client, 'o/r', self.args('--body', text, *flags))

    def writes(self):
        return [c for c in self.client.calls if c[0] != 'GET']

    def test_edit_own_not_newest_other_author_and_verify(self):
        self.client.rows[1]['created_at'] = '2099-01-01T00:00:00Z'
        result = self.call(' Привет\r\n$HOME $(literal)\n')
        self.assertTrue(result['changed'])
        self.assertEqual(result['comment_id'], 55)
        self.assertEqual([(c[0], c[1]) for c in self.writes()],
                         [('PATCH', 'repos/o/r/issues/9/comments/55')])
        self.assertEqual(self.client.rows[1]['body'], 'old')
        self.assertEqual(self.client.calls[-1][1], 'repos/o/r/issues/comments/55')

    def test_creation_time_not_update_time_or_catalog_order_and_id_tie(self):
        old = self.client.row(100, 3, '2026-10-19T00:00:00Z')
        old['updated_at'] = '2099-01-01T00:00:00Z'
        self.client.rows = [self.client.row(60, 3), old, self.client.row(59, 3)]
        self.assertEqual(self.call()['comment_id'], 60)
        self.assertEqual(self.client.rows[1]['body'], 'old')

    def test_timestamp_offsets_compare_as_instants(self):
        self.client.rows = [self.client.row(55, 3, '2026-10-20T12:00:00+03:00'),
                            self.client.row(54, 3, '2026-10-20T10:00:00Z')]
        self.assertEqual(self.call()['comment_id'], 54)

    def test_absent_own_never_posts(self):
        for rows in ([], [self.client.row(56, 4)]):
            self.client = Fake()
            self.client.rows = rows
            with self.assertRaises(GVError) as caught:
                self.call()
            self.assertEqual(caught.exception.code, 'comment_not_found')
            self.assertFalse(caught.exception.details['mutation_attempted'])
            self.assertEqual(self.writes(), [])

    def test_equal_body_rechecks_parent_and_catalog_no_write(self):
        self.assertFalse(self.call('old')['changed'])
        self.assertEqual(self.writes(), [])
        self.assertEqual(sum(c[1].endswith('/9/comments') for c in self.client.calls), 2)
        self.assertEqual(sum(c[1].endswith('/pulls/9') for c in self.client.calls), 2)

    def test_file_and_stdin_preserve_text(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'body.md'
            file.write_bytes(' File Привет\r\n'.encode())
            result = discussion_comment(self.client, 'o/r', self.args('-F', str(file)))
            self.assertEqual(result['body'], ' File Привет\r\n')
        with patch('sys.stdin', io.StringIO('stdin\r\n')):
            result = discussion_comment(self.client, 'o/r', self.args('--body-file', '-'))
        self.assertEqual(result['body'], 'stdin\r\n')

    def test_invalid_combinations_refuse_before_client_or_selector(self):
        for selector in ('9', 'branch'):
            for flags in (['--attach', 'missing'], ['--reply-to', '1'], ['--path', 'a'],
                          ['--line', '1'], ['--commit', 'a'*40], ['--yes'],
                          ['--delete-last', '--yes']):
                args = parser().parse_args(['pr', 'comment', selector, '-R', 'o/r',
                                           '--edit-last', '--body', 'new', '--dry-run', *flags])
                with patch('gitverse_cli.cli.Client', side_effect=AssertionError('client')):
                    with self.assertRaises(GVError) as caught:
                        execute(args)
                self.assertEqual(caught.exception.code, 'usage_error')

    def test_missing_blank_invalid_and_oversize_body_before_network(self):
        for text in (None, '', ' ', '\0', '\ud800', 'x'*1048577):
            with self.assertRaises(GVError) as caught:
                self.call(text)
            self.assertEqual(caught.exception.code, 'usage_error')
        self.assertEqual(self.client.calls, [])

    def test_offline_plan_does_not_predict_id_or_creation(self):
        self.client.dry_run = True
        result = self.call()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(result['method'], 'select PATCH')
        self.assertFalse(result['create_if_none'])
        self.assertNotIn('comment_id', result)
        self.assertNotIn('POST', result['plan'])
        self.assertIn('comment_not_found', result['plan'])

    def test_invalid_catalog_dto_parent_author_and_time_refuse(self):
        base = self.client.rows[0]
        bads = [None, {}, [None], [base, base]]
        for key, value in [('id', True), ('user', {}), ('user', {'id': True}),
                           ('body', None), ('updated_at', None), ('created_at', 'yesterday'),
                           ('created_at', '2026-10-20T10:00:00'),
                           ('issue_url', 'https://other.invalid/repos/o/r/issues/9'),
                           ('issue_url', self.client.base+'/repos/o/r/issues/10')]:
            row = copy.deepcopy(base); row[key] = value; bads.append([row])
        for bad in bads:
            self.client = Fake(); original = self.client.request
            def request(method, path, **kwargs):
                if method == 'GET' and path.endswith('/9/comments'):
                    return bad
                return original(method, path, **kwargs)
            self.client.request = request
            with self.subTest(bad=bad):
                with self.assertRaises(GVError): self.call()
                self.assertEqual(self.writes(), [])

    def test_wrong_user_or_pr_identity_never_writes(self):
        for target in ('user', 'parent'):
            self.client = Fake(); original = self.client.request
            def request(method, path, **kwargs):
                result = original(method, path, **kwargs)
                if target == 'user' and path == 'user': return {'id': True, 'login': 'me'}
                if target == 'parent' and path.endswith('/pulls/9'): result['number'] = 10
                return result
            self.client.request = request
            with self.assertRaises(GVError): self.call()
            self.assertEqual(self.writes(), [])

    def test_readback_wrong_author_parent_id_and_text_retains_recovery(self):
        for key, value in [('user', {'id': 4}), ('id', 56), ('body', 'ignored'),
                           ('issue_url', self.client.base+'/repos/other/r/issues/9')]:
            self.client = Fake(); count = 0; original = self.client.request
            def request(method, path, **kwargs):
                nonlocal count
                result = original(method, path, **kwargs)
                if method == 'GET' and path.endswith('/comments/55'):
                    count += 1
                    if count == 2: result[key] = value
                return result
            self.client.request = request
            with self.assertRaises(GVError) as caught: self.call()
            self.assertEqual(caught.exception.details['comment_id'], 55)
            self.assertEqual(caught.exception.details['stage'], 'verify')
            self.assertTrue(caught.exception.details['mutation_attempted'])
            self.assertEqual(len(self.writes()), 1)

    def test_new_own_or_selected_change_in_final_catalog_even_noop_refuses(self):
        for change in ('new', 'body', 'removed'):
            self.client = Fake(); count = 0
            def hook(client, method, path):
                nonlocal count
                if method == 'GET' and path.endswith('/9/comments'):
                    count += 1
                    if count == 2:
                        if change == 'new': client.rows.append(client.row(88, 3))
                        elif change == 'body': client.rows[0]['body'] = 'concurrent'
                        else: client.rows.pop(0)
            self.client.hook = hook
            with self.assertRaises(GVError) as caught: self.call('old')
            self.assertEqual(caught.exception.code, 'concurrent_change')
            self.assertFalse(caught.exception.details['mutation_attempted'])
            self.assertEqual(self.writes(), [])

    def test_permission_and_transport_catalog_failures_never_write(self):
        for code, status in [('http_error', 401), ('http_error', 403), ('http_error', 404),
                             ('incomplete_response', None), ('response_limit', None),
                             ('deadline_exceeded', None), ('pagination_limit', None)]:
            self.client = Fake()
            def hook(client, method, path):
                if path.endswith('/9/comments'): raise GVError('refuse', code, status)
            self.client.hook = hook
            with self.assertRaises(GVError) as caught: self.call()
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(self.writes(), [])

    def test_entry_bound_refuses_selection(self):
        with self.assertRaises(GVError) as caught: self.call('new', '--max-entries', '1')
        self.assertEqual(caught.exception.code, 'catalog_limit')
        self.assertEqual(self.writes(), [])

    def test_unknown_write_once_retains_id_and_no_post(self):
        for error in (GVError('timeout', 'network_error'), OSError('private'),
                      http.client.IncompleteRead(b'')):
            self.client = Fake(); self.client.fail = error
            with self.assertRaises(GVError) as caught: self.call()
            self.assertEqual(caught.exception.details['repository'], 'o/r')
            self.assertEqual(caught.exception.details['comment_id'], 55)
            self.assertEqual(caught.exception.details['stage'], 'edit')
            self.assertTrue(caught.exception.details['mutation_attempted'])
            self.assertEqual([c[0] for c in self.writes()], ['PATCH'])

    def test_shared_deadline_complete_bounded_unpaginated_catalogs(self):
        self.call()
        self.assertEqual(len({c[4]['deadline'] for c in self.client.calls}), 1)
        self.assertTrue(all(c[4]['require_complete'] and c[4]['max_response_bytes'] == 33554432
                            for c in self.client.calls))
        self.assertTrue(all(c[2] is None for c in self.client.calls))

    def test_web_auth_refuses_without_requests(self):
        self.client.backend = 'web'
        with self.assertRaises(GVError): self.call()
        self.assertEqual(self.client.calls, [])

    def test_real_transport_redirect_refuses_without_forward_or_retry(self):
        class Response(io.BytesIO): status = 200
        client = Client(dry_run=True); client.dry_run = False
        client.headers['Authorization'] = 'Bearer synthetic-test-token'
        client.opener = Mock()
        client.opener.open.side_effect = [Response(json.dumps(row).encode()) for row in (
            self.client.parent, {'id': 3, 'login': 'me'}, self.client.rows,
            self.client.rows[0], self.client.rows, self.client.parent)] + [
                urllib.error.HTTPError(client.base, 307, 'refused',
                                       {'Location': 'https://other.invalid/x'}, None)]
        with self.assertRaises(GVError) as caught:
            discussion_comment(client, 'o/r', self.args('--body', 'new'))
        self.assertEqual(caught.exception.status, 307)
        self.assertEqual(caught.exception.details['comment_id'], 55)
        self.assertEqual(client.opener.open.call_count, 7)
        self.assertEqual([c.args[0].method for c in client.opener.open.call_args_list].count('PATCH'), 1)
        for call in client.opener.open.call_args_list:
            self.assertTrue(call.args[0].full_url.startswith(client.base+'/'))
            self.assertIsNone(call.args[0].get_header('Cookie'))
        self.assertIsNone(NoRedirect().redirect_request(None, None, 307, '', {}, 'https://other.invalid/x'))

    def test_discovery_describes_exact_flag(self):
        result = execute(parser().parse_args(['agent-info']))['discussion_comment_edit_last']
        self.assertIn('--edit-last', result['command'])
        self.assertIn('absence errors', result['behavior'])
