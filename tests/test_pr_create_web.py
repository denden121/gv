import copy
import http.client
import os
import time
import unittest
from unittest.mock import Mock, patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from gitverse_cli.pr_create_web import web_create


class WebCreateTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        self.client = Mock(backend='public', dry_run=False)
        self.row = dict(id=3, full_name='o/r', private=True, archived=False)
        self.client.request.side_effect = lambda *a, **kw: copy.deepcopy(self.row)
        self.launch = patch('gitverse_cli.pr_create_web.open_browser')
        self.browser = self.launch.start(); self.addCleanup(self.launch.stop)
        self.context = patch('gitverse_cli.repo_default.context', return_value=None)
        self.context.start(); self.addCleanup(self.context.stop)
        self.base_config = patch('gitverse_cli.pr_create.configured_base', return_value=None)
        self.base_config.start(); self.addCleanup(self.base_config.stop)

    def args(self, *flags):
        return parser().parse_args(['pr', 'create', '-R', 'o/r', '--web', *flags])

    def call(self, *flags):
        return web_create(self.client, 'o/r', self.args(*flags))

    def test_creation_form_without_required_title_or_branches(self):
        result = self.call()
        self.assertEqual(result['url'], 'https://gitverse.ru/o/r/compare')
        self.assertEqual(result['repository_id'], 3)
        self.assertTrue(result['launcher_acknowledged'])
        self.assertFalse(result['pull_request_created'])
        self.assertFalse(result['form_verified'])
        self.assertFalse(result['branches_selected'])
        self.assertFalse(result['mutation_attempted'])
        self.assertEqual(self.client.request.call_count, 2)
        for call in self.client.request.call_args_list:
            self.assertEqual(call.args, ('GET', 'repos/o/r'))
            self.assertTrue(call.kwargs['require_complete'])
            self.assertEqual(call.kwargs['max_response_bytes'], 33554432)
            self.assertIn('deadline', call.kwargs)
        self.browser.assert_called_once()

    def test_short_alias_and_execute_dispatch(self):
        with patch('gitverse_cli.cli.Client', return_value=self.client):
            result = execute(parser().parse_args(['pr', 'create', '-w', '-R', 'o/r']))
        self.assertTrue(result['browser_launched'])

    def test_dry_run_offline_no_input_auth_browser_or_network(self):
        with patch('gitverse_cli.client.load_auth', side_effect=AssertionError('auth')), \
             patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')), \
             patch('subprocess.Popen', side_effect=AssertionError('process')):
            result = execute(self.args('--dry-run'))
        self.assertFalse(result['repository_verified'])
        self.assertFalse(result['form_verified'])
        self.assertFalse(result['browser_launched'])
        self.assertEqual(result['mutation_outcome'], 'none')
        self.browser.assert_not_called()

    def test_native_conflicts_rejected_before_client_or_body_read(self):
        for flags in (['--editor'], ['--draft'], ['--reviewer', 'me'],
                      ['--no-maintainer-edit'], ['--attach', '/missing']):
            with self.subTest(flags=flags), patch('gitverse_cli.cli.Client', side_effect=AssertionError('auth')):
                with self.assertRaises(GVError) as caught: execute(self.args(*flags))
                self.assertEqual(caught.exception.code, 'usage_error')

    def test_unverified_seeds_refuse_even_empty_never_silently_discarded(self):
        for flags in (['--title', ''], ['--body', ''],
                      ['--body-file', '-'], ['--template', '/missing'], ['--assignee', '@me'],
                      ['--label', 'x'], ['--milestone', 'x'], ['--project', 'x'],
                      ['--recover', '/missing'], ['--fill'], ['--fill-first'], ['--fill-verbose']):
            for preview in ([], ['--dry-run']):
                with self.subTest(flags=flags, preview=preview), patch('gitverse_cli.cli.Client', side_effect=AssertionError('auth')):
                    with self.assertRaises(GVError) as caught: execute(self.args(*flags, *preview))
                    self.assertEqual(caught.exception.code, 'feature_unavailable')
                    self.assertEqual(caught.exception.details['mutation_outcome'], 'none')
        self.browser.assert_not_called()

    def branch_rows(self, base='main', head='feature/Б#&'):
        self.row['default_branch'] = base
        def read(method, path, **kwargs):
            if path == 'repos/o/r': return copy.deepcopy(self.row)
            from urllib.parse import unquote
            name = unquote(path.split('/branches/')[1])
            return {'name': name, 'commit': {'sha': ('a' if name == base else 'b') * 40}}
        self.client.request.side_effect = read
        return read

    def test_explicit_branch_prefill_encodes_native_query_and_rechecks(self):
        self.branch_rows()
        result = self.call('--head', 'feature/Б#&', '--base', 'main')
        self.assertEqual(result['url'], 'https://gitverse.ru/o/r/compare?baseBranch=main&headBranch=feature%2F%D0%91%23%26')
        self.assertTrue(result['branches_verified'])
        self.assertTrue(result['branches_prefilled'])
        self.assertFalse(result['branches_selected'])
        self.assertEqual(result['branch_shas'], {'base': 'a'*40, 'head': 'b'*40})
        self.assertEqual(self.client.request.call_count, 6)
        self.assertTrue(all(c.args[0] == 'GET' and c.kwargs['require_complete'] for c in self.client.request.call_args_list))

    def test_head_uses_verified_default_base(self):
        self.branch_rows()
        result = self.call('--head', 'feature/Б#&')
        self.assertEqual(result['base'], 'main')
        self.assertIn('baseBranch=main', result['url'])

    def test_branch_prefill_dry_run_offline_with_unresolved_default(self):
        self.client.dry_run = True
        for flags, resolved in ((['--head', 'feature'], False), (['--head', 'feature', '--base', 'main'], True)):
            result = self.call(*flags)
            self.assertEqual(result['base_resolved'], resolved)
            self.assertFalse(result['branches_verified'])
            self.assertIn('headBranch=feature', result['url'])
        self.client.request.assert_not_called(); self.browser.assert_not_called()

    def test_invalid_branch_and_cross_repository_prefill_refuse(self):
        for flags, code in ((['--head', 'bad..name'], 'usage_error'), (['--base', ''], 'usage_error'),
                            (['--head', 'someone:feature'], 'feature_unavailable')):
            with self.subTest(flags=flags), patch('gitverse_cli.cli.Client', side_effect=AssertionError('auth')):
                with self.assertRaises(GVError) as caught: execute(self.args(*flags))
                self.assertEqual(caught.exception.code, code)

    def test_branch_dto_scope_sha_and_limits_refuse_without_launch(self):
        for value in (None, [], {}, {'name': 'wrong', 'commit': {'sha': 'a'*40}},
                      {'name': 'main', 'commit': []}, {'name': 'main', 'commit': {'sha': 'oops'}},
                      GVError('denied', 'http_error', 403), GVError('large', 'response_too_large'),
                      GVError('partial', 'incomplete_response')):
            self.client.request.side_effect = [self.row, value] if not isinstance(value, Exception) else [self.row, value]
            with self.subTest(value=value), self.assertRaises(GVError) as caught:
                self.call('--base', 'main', '--head', 'feature')
            self.assertEqual(caught.exception.details['mutation_outcome'], 'none')
        self.browser.assert_not_called()

    def test_concurrent_branch_or_default_change_refuses(self):
        read = self.branch_rows(head='feature')
        for default in (True, False):
            count = 0
            def changing(method, path, **kwargs):
                nonlocal count
                count += 1
                row = read(method, path, **kwargs)
                if default and count == 4: row['default_branch'] = 'other'
                if not default and count == 5: row['commit']['sha'] = 'c'*40
                return row
            self.client.request.side_effect = changing
            with self.assertRaises(GVError) as caught: self.call('--head', 'feature')
            self.assertEqual(caught.exception.code, 'concurrent_change')
        self.browser.assert_not_called()

    def test_implicit_current_head_requires_matching_remote_and_rechecks(self):
        self.branch_rows(head='feature')
        with patch('gitverse_cli.repo_default.context', return_value='/synthetic/.git'), \
             patch('gitverse_cli.pr_create_fill.remote_for', return_value='origin') as remote, \
             patch('gitverse_cli.pr_create.select_head', return_value=('feature', None)) as head:
            self.assertEqual(self.call()['head'], 'feature')
        self.assertEqual(remote.call_count, 2); self.assertEqual(head.call_count, 2)
        self.browser.reset_mock()
        with patch('gitverse_cli.repo_default.context', return_value='/synthetic/.git'), \
             patch('gitverse_cli.pr_create_fill.remote_for', side_effect=GVError('scope', 'ambiguous_remote')):
            with self.assertRaises(GVError): self.call()
        self.browser.assert_not_called()

    def test_changed_local_head_refuses(self):
        self.branch_rows(head='feature')
        with patch('gitverse_cli.repo_default.context', return_value='/synthetic/.git'), \
             patch('gitverse_cli.pr_create_fill.remote_for', return_value='origin'), \
             patch('gitverse_cli.pr_create.select_head', side_effect=[('feature', None), ('changed', None)]):
            with self.assertRaises(GVError) as caught: self.call()
        self.assertEqual(caught.exception.code, 'concurrent_change'); self.browser.assert_not_called()

    def test_web_backend_refuses_before_cookie_file_read(self):
        with patch('gitverse_cli.cli.Client', side_effect=AssertionError('cookies')):
            with self.assertRaises(GVError) as caught: execute(self.args('--backend', 'web'))
        self.assertEqual(caught.exception.code, 'backend_unsupported')

    def test_malformed_or_wrong_scoped_dtos_refuse(self):
        for row in (None, [], {}, dict(self.row, id=True), dict(self.row, id=0),
                    dict(self.row, full_name='evil/r'), dict(self.row, private=None),
                    dict(self.row, archived=None)):
            self.client.request.side_effect = None; self.client.request.return_value = row
            with self.subTest(row=row), self.assertRaises(GVError): self.call()
        self.browser.assert_not_called()

    def test_archived_refuses(self):
        for key in ('archived',):
            self.client.request.side_effect = None
            self.client.request.return_value = dict(self.row, **{key: True})
            with self.assertRaises(GVError) as caught: self.call()
            self.assertEqual(caught.exception.code, 'repository_unavailable')
        self.browser.assert_not_called()

    def test_concurrent_identity_or_state_change_refuses(self):
        for key, value in (('id', 4), ('private', False), ('archived', True)):
            self.client.request.side_effect = [self.row, dict(self.row, **{key: value})]
            with self.assertRaises(GVError) as caught: self.call()
            self.assertEqual(caught.exception.code, 'concurrent_change')
        self.browser.assert_not_called()

    def test_denied_missing_redirect_partial_or_transport_no_retry(self):
        for error in ([GVError('error', 'http_error', n) for n in (401, 403, 404, 302, 307)] +
                      [GVError('large', 'response_too_large'), GVError('partial', 'incomplete_response'),
                       OSError('private data'), http.client.IncompleteRead(b'private')]):
            self.client.request.reset_mock(); self.client.request.side_effect = error
            with self.assertRaises(GVError) as caught: self.call()
            self.assertFalse(caught.exception.details['mutation_attempted'])
            self.assertEqual(caught.exception.details['mutation_outcome'], 'none')
            self.assertEqual(self.client.request.call_count, 1)
        self.browser.assert_not_called()

    def test_shared_deadline_prevents_open(self):
        with patch('gitverse_cli.pr_create_web.time.monotonic', side_effect=[1, 100]):
            with self.assertRaises(GVError) as caught: self.call('--timeout', '1')
        self.assertEqual(caught.exception.code, 'deadline_exceeded')
        self.client.request.assert_not_called(); self.browser.assert_not_called()

    def test_browser_unknown_handoff_is_not_unknown_api_mutation(self):
        self.browser.side_effect = GVError('launcher', 'browser_error', details={'browser_handoff': 'unknown'})
        with self.assertRaises(GVError) as caught: self.call()
        self.assertEqual(caught.exception.details['browser_handoff'], 'unknown')
        self.assertEqual(caught.exception.details['mutation_outcome'], 'none')
        self.assertEqual(caught.exception.details['url'], 'https://gitverse.ru/o/r/compare')
        self.browser.assert_called_once()

    def test_returned_external_url_is_never_used(self):
        self.row['html_url'] = 'https://evil.invalid/leak'
        self.row['clone_url'] = 'https://evil.invalid/token'
        self.assertEqual(self.call()['url'], 'https://gitverse.ru/o/r/compare')
        self.assertEqual(self.browser.call_args.args[0], 'https://gitverse.ru/o/r/compare')

    def test_actual_launcher_environment_is_credential_free(self):
        from gitverse_cli.comment_web import open_browser
        with patch.dict(os.environ, {'GV_BROWSER': 'test-browser --new', 'GV_TOKEN': 'secret',
                  'GITVERSE_TOKEN': 'secret', 'GITVERSE_COOKIE_FILE': '/secret', 'GITVERSE_CSRF_TOKEN': 'secret'}), \
             patch('gitverse_cli.comment_web.subprocess.Popen') as launch:
            launch.return_value.wait.return_value = 0
            open_browser('https://gitverse.ru/o/r/compare', time.monotonic()+5)
        argv = launch.call_args.args[0]
        self.assertEqual(argv, ['test-browser', '--new', 'https://gitverse.ru/o/r/compare'])
        self.assertFalse(any(k in launch.call_args.kwargs['env'] for k in
                         ('GV_TOKEN', 'GITVERSE_TOKEN', 'GITVERSE_COOKIE_FILE', 'GITVERSE_CSRF_TOKEN')))

    def test_no_api_credential_forwarded_on_actual_redirect(self):
        import io
        import urllib.error
        from urllib.request import Request
        from gitverse_cli.client import NoRedirect
        request = Request('https://api.gitverse.ru/repos/o/r', headers={'Authorization': 'Bearer secret'})
        self.assertIsNone(NoRedirect().redirect_request(request, None, 302, '', {}, 'https://evil.invalid/'))
        client = Client(dry_run=True); client.dry_run = False; client.opener = Mock()
        client.opener.open.side_effect = urllib.error.HTTPError(request.full_url, 302, '',
                           {'Location': 'https://evil.invalid/'}, io.BytesIO(b''))
        with self.assertRaises(GVError): web_create(client, 'o/r', self.args())
        client.opener.open.assert_called_once(); self.browser.assert_not_called()
