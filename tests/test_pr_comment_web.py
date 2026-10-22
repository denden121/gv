import copy
import http.client
import os
import subprocess
import time
import unittest
from unittest.mock import Mock, patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.comment_web import open_browser, web_comment
from test_pr_comment import Fake


class WebCommentTests(unittest.TestCase):
    def setUp(self):
        self.client = Fake()
        self.client.parent['html_url'] = 'https://gitverse.ru/o/r/pulls/9'
        self.launch = patch('gitverse_cli.comment_web.open_browser')
        self.browser = self.launch.start()
        self.addCleanup(self.launch.stop)

    def call(self, *flags):
        args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', '--web', *flags])
        return web_comment(self.client, args)

    def test_new_opens_pr_without_post(self):
        r = self.call()
        self.assertEqual(r['url'], 'https://gitverse.ru/o/r/pulls/9')
        self.assertTrue(r['launcher_acknowledged'])
        self.assertFalse(r['comment_submitted'])
        self.assertEqual([c[0] for c in self.client.calls], ['GET', 'GET'])
        self.browser.assert_called_once()

    def test_edit_opens_own_native_anchor_not_returned_issue_or_external_url(self):
        self.client.rows[0]['html_url'] = 'https://evil.invalid/token'
        r = self.call('--edit-last')
        self.assertEqual(r['comment_id'], 55)
        self.assertEqual(r['url'], 'https://gitverse.ru/o/r/pulls/9#comment-55')
        self.assertTrue(all(c[0] == 'GET' for c in self.client.calls))
        self.assertTrue(all(not c[1].startswith('https:') for c in self.client.calls))
        catalogs = [c for c in self.client.calls if c[1].endswith('/issues/9/comments')]
        self.assertEqual(len(catalogs), 2)
        self.assertTrue(all(c[2] is None for c in catalogs))

    def test_newest_by_creation_then_id_not_updated_or_other_author(self):
        self.client.rows += [self.client.row(57, 3), self.client.row(58, 4, '2027-01-01T00:00:00Z')]
        self.client.rows[0]['updated_at'] = '2099'
        self.client.rows.reverse()
        self.assertEqual(self.call('--edit-last')['comment_id'], 57)

    def test_absence_and_fallback(self):
        self.client.rows = [self.client.row(56, 4)]
        with self.assertRaises(GVError) as e: self.call('--edit-last')
        self.assertEqual(e.exception.code, 'comment_not_found')
        self.browser.assert_not_called()
        r = self.call('--edit-last', '--create-if-none')
        self.assertIsNone(r['comment_id'])
        self.assertEqual(r['mode'], 'create')
        self.assertEqual(r['url'], 'https://gitverse.ru/o/r/pulls/9')
        self.assertTrue(all(c[0] == 'GET' for c in self.client.calls))

    def test_fallback_with_existing_opens_comment_without_write(self):
        self.assertEqual(self.call('--edit-last', '--create-if-none')['comment_id'], 55)
        self.assertTrue(all(c[0] == 'GET' for c in self.client.calls))

    def test_invalid_combinations_before_client_input_or_selection(self):
        flags = [('--body', 'x'), ('--body-file', '/missing'), ('--body-file', '-'),
                 ('--editor',), ('--attach', '/missing'), ('--delete-last', '--yes'),
                 ('--path', 'x'), ('--line', '1'), ('--reply-to', '1'), ('--commit', 'a'*40),
                 ('--yes',), ('--create-if-none',), ('--backend', 'web')]
        for extra in flags:
            with self.subTest(extra=extra), patch('gitverse_cli.cli.Client', side_effect=AssertionError('client touched')):
                with self.assertRaises(GVError):
                    execute(parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', '-w', *extra]))
        self.browser.assert_not_called()

    def test_dry_run_no_network_or_browser_and_unknown_selection(self):
        self.client.dry_run = True
        self.client.request = Mock(side_effect=AssertionError('network'))
        for flags in ((), ('--edit-last',), ('--edit-last', '--create-if-none')):
            r = self.call(*flags)
            self.assertFalse(r['browser_launched'])
            self.assertFalse(r['url_verified'])
            self.assertIsNone(r['comment_id'])
        self.browser.assert_not_called()

    def test_url_and_short_alias_through_execute(self):
        with patch('gitverse_cli.cli.Client', return_value=self.client):
            r = execute(parser().parse_args(['pr', 'comment', 'https://gitverse.ru/o/r/pulls/9', '-w']))
        self.assertEqual(r['url'], 'https://gitverse.ru/o/r/pulls/9')

    def test_untrusted_input_and_returned_urls_refuse(self):
        for selector in ('https://evil.invalid/o/r/pulls/9', 'https://gitverse.ru/o/r/pulls/9?token=x',
                         'https://gitverse.ru/o/r/pulls/9#bad', 'https://gitverse.ru/o/r/pulls/9\n'):
            with self.assertRaises(GVError):
                web_comment(self.client, parser().parse_args(['pr','comment',selector,'--web']))
        for url in ('https://evil.invalid/o/r/pulls/9', 'https://user:pass@gitverse.ru/o/r/pulls/9',
                    'https://gitverse.ru/o/r/pulls/8', 'https://gitverse.ru/o/r/pulls/9?token=x', None):
            self.client.parent['html_url'] = url
            with self.assertRaises(GVError): self.call()
        self.browser.assert_not_called()

    def test_malformed_or_wrong_parent_dto_refuses(self):
        for key, value in (('id', True), ('number', 8), ('base', {'sha':'b'*40,'repo':{'id':2,'full_name':'other/r'}})):
            self.client = Fake(); self.client.parent['html_url'] = 'https://gitverse.ru/o/r/pulls/9'
            self.client.parent[key] = value
            with self.assertRaises(GVError): self.call()
        self.browser.assert_not_called()

    def test_bad_comment_catalogs_refuse_not_fallback(self):
        original = copy.deepcopy(self.client.rows)
        for change in ({'id': True}, {'issue_url': 'https://evil.invalid/repos/o/r/issues/9'},
                       {'user': {'id':None}}, {'created_at': None}, {'body':None}):
            self.client.rows = copy.deepcopy(original)
            self.client.rows[0].update(change)
            with self.assertRaises(GVError): self.call('--edit-last', '--create-if-none')
        self.client.rows = [original[0], original[0]]
        with self.assertRaises(GVError): self.call('--edit-last')
        self.browser.assert_not_called()

    def test_permission_and_transport_errors_do_not_open_or_retry(self):
        for error in (GVError('denied','http_error',401), GVError('denied','http_error',403),
                      GVError('redirect','http_error',302), GVError('missing','http_error',404),
                      GVError('partial','incomplete_response'), http.client.IncompleteRead(b''), OSError('secret')):
            self.client.request = Mock(side_effect=error)
            with self.assertRaises(GVError): self.call()
            self.assertEqual(self.client.request.call_count, 1)
        self.browser.assert_not_called()

    def test_user_dto_and_comment_id_get_wrong_ownership_refuse(self):
        request = self.client.request
        for target, bad in (('user', {'id':True,'login':'me'}),
                            ('repos/o/r/issues/comments/55', self.client.row(55,4))):
            def altered(method,path,query=None,**kw):
                return bad if path == target else request(method,path,query,**kw)
            self.client.request = altered
            with self.assertRaises(GVError): self.call('--edit-last')
        self.browser.assert_not_called()

    def test_entry_and_deadline_bounds(self):
        with self.assertRaises(GVError) as e: self.call('--edit-last', '--max-entries', '1')
        self.assertEqual(e.exception.code, 'catalog_limit')
        for c in self.client.calls:
            self.assertTrue(c[4]['require_complete'])
            self.assertEqual(c[4]['max_response_bytes'],33554432)
            self.assertIn('deadline', c[4])
        with patch('gitverse_cli.comment_web.time.monotonic', side_effect=[1,100]):
            with self.assertRaises(GVError) as e: self.call('--timeout','1')
        self.assertEqual(e.exception.code, 'deadline_exceeded')
        self.browser.assert_not_called()

    def test_concurrent_catalog_and_parent_refuse_handoff(self):
        for kind in ('catalog','parent','selected'):
            self.client = Fake(); self.client.parent['html_url'] = 'https://gitverse.ru/o/r/pulls/9'
            self.browser.reset_mock(); count = 0
            def hook(c, method, path):
                nonlocal count
                suffix = {'catalog':'/issues/9/comments','parent':'/pulls/9','selected':'/issues/comments/55'}[kind]
                if path.endswith(suffix):
                    count += 1
                    if (kind == 'catalog' and count == 2): c.rows.append(c.row(99,3))
                    if (kind == 'parent' and count == 2): c.parent['updated_at'] = 'v2'
                    if kind == 'selected': c.rows[0]['body'] = 'changed'
            self.client.hook = hook
            with self.assertRaises(GVError) as e: self.call('--edit-last')
            self.assertEqual(e.exception.code, 'concurrent_change')
            self.browser.assert_not_called()

    def test_launcher_failure_preserves_recovery_without_mutation(self):
        self.browser.side_effect = GVError('launcher','browser_error',details={'browser_handoff':'unknown'})
        with self.assertRaises(GVError) as e:self.call('--edit-last')
        self.assertEqual(e.exception.details['comment_id'],55)
        self.assertFalse(e.exception.details['mutation_attempted'])
        self.assertEqual(e.exception.details['browser_handoff'],'unknown')
        self.browser.assert_called_once()

    def test_branch_selection_bounded_and_ambiguous_refuses(self):
        args = parser().parse_args(['pr','comment','feature','-R','o/r','--web'])
        self.client.parent['state']='open'; self.client.parent['head']['ref']='feature'
        request = self.client.request
        def branch(method,path,query=None,**kw):
            if path.endswith('/pulls'):
                return [copy.deepcopy(self.client.parent)] if query['page']==1 else []
            return request(method,path,query,**kw)
        self.client.request=branch
        r=web_comment(self.client,args); self.assertEqual(r['number'],9)
        self.browser.reset_mock()
        def ambiguous(method,path,query=None,**kw):
            if path.endswith('/pulls'):
                return [self.client.parent,{**self.client.parent,'number':10}] if query['page']==1 else []
            return request(method,path,query,**kw)
        self.client.request=ambiguous
        with self.assertRaises(GVError):web_comment(self.client,args)
        self.browser.assert_not_called()

    def test_branch_entry_and_page_limits(self):
        args=parser().parse_args(['pr','comment','feature','-R','o/r','--web','--max-entries','1'])
        self.client.parent.update(state='open');self.client.parent['head']['ref']='feature'
        self.client.request=Mock(return_value=[self.client.parent,{**self.client.parent,'number':10}])
        with self.assertRaises(GVError) as e:web_comment(self.client,args)
        self.assertEqual(e.exception.code,'catalog_limit')
        args.max_entries=100000
        self.client.request=lambda m,p,q=None,**kw:[{**self.client.parent,'number':q['page']}]
        with self.assertRaises(GVError) as e:web_comment(self.client,args)
        self.assertEqual(e.exception.code,'pagination_limit')
        self.browser.assert_not_called()


class LauncherTests(unittest.TestCase):
    def test_shell_free_null_streams_and_credential_environment(self):
        process=Mock();process.wait.return_value=0
        env={'GV_BROWSER':'browser --new-window','GITVERSE_TOKEN':'secret','GV_TOKEN':'secret',
             'GITVERSE_COOKIE_FILE':'secret','GITVERSE_CSRF_TOKEN':'secret','DISPLAY':':0'}
        with patch.dict(os.environ,env,clear=True), patch('gitverse_cli.comment_web.subprocess.Popen',return_value=process) as launch:
            open_browser('https://gitverse.ru/o/r/pulls/9',time.monotonic()+2)
        a,k=launch.call_args
        self.assertEqual(a[0],['browser','--new-window','https://gitverse.ru/o/r/pulls/9'])
        self.assertNotIn('shell',k)
        self.assertFalse(any(key in k['env'] for key in env if key.startswith('GITVERSE') or key=='GV_TOKEN'))
        self.assertEqual(k['stdout'],subprocess.DEVNULL)
        self.assertEqual(k['stderr'],subprocess.DEVNULL)
        self.assertEqual(k['stdin'],subprocess.DEVNULL)

    def test_invalid_config_and_start_failures_are_sanitized(self):
        for value in ('"unterminated','x'*8193):
            with patch.dict(os.environ,{'GV_BROWSER':value}), patch('gitverse_cli.comment_web.subprocess.Popen') as p:
                with self.assertRaises(GVError) as e:open_browser('https://gitverse.ru',time.monotonic()+1)
                self.assertEqual(e.exception.code,'usage_error');p.assert_not_called()
        with patch('gitverse_cli.comment_web.subprocess.Popen',side_effect=OSError('sensitive')):
            with self.assertRaises(GVError) as e:open_browser('https://gitverse.ru',time.monotonic()+1)
            self.assertNotIn('sensitive',str(e.exception))

    def test_launcher_failure_timeout_and_expired_deadline(self):
        process=Mock(pid=123);process.wait.return_value=1
        with patch('gitverse_cli.comment_web.subprocess.Popen',return_value=process) as p:
            with self.assertRaises(GVError):open_browser('https://gitverse.ru',time.monotonic()+1)
            self.assertEqual(p.call_count,1)
        process.wait.side_effect=[subprocess.TimeoutExpired('browser',1),0]
        with patch('gitverse_cli.comment_web.subprocess.Popen',return_value=process),patch('gitverse_cli.comment_web.os.killpg') as kill:
            with self.assertRaises(GVError) as e:open_browser('https://gitverse.ru',time.monotonic()+1)
            self.assertEqual(e.exception.details['browser_handoff'],'unknown');kill.assert_called_once()
        with patch('gitverse_cli.comment_web.subprocess.Popen') as p:
            with self.assertRaises(GVError):open_browser('https://gitverse.ru',time.monotonic()-1)
            p.assert_not_called()
