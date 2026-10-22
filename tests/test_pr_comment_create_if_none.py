"""Create-if-none is an own-last edit fallback, never a deduplication flag."""
import copy
import http.client
import io
import time
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.pr_comment import discussion_comment, newest_own
from test_pr_comment import Fake


class CreateIfNoneTests(unittest.TestCase):
    def setUp(self): self.client = Fake()

    def call(self, *flags, text='new'):
        args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', '--edit-last',
                                    '--create-if-none', '--body', text, *flags])
        args.number = 9
        return discussion_comment(self.client, 'o/r', args)

    def writes(self): return [c for c in self.client.calls if c[0] != 'GET']

    def test_empty_creates_once_with_independent_readback(self):
        self.client.rows = []
        row = self.call(text=' Привет\r\n')
        self.assertEqual(row['body'], ' Привет\r\n')
        self.assertEqual([x[0] for x in self.writes()], ['POST'])
        catalogs = [c for c in self.client.calls if c[1].endswith('/9/comments') and c[0] == 'GET']
        self.assertEqual(len(catalogs), 2)
        self.assertTrue(all(c[2] is None for c in catalogs))
        self.assertEqual(self.client.calls[-1][1], 'repos/o/r/issues/comments/99')

    def test_other_user_only_still_creates(self):
        self.client.rows = [self.client.row(56, 4)]
        self.assertEqual(self.call()['id'], 99)
        self.assertEqual(self.client.rows[0]['body'], 'old')

    def test_existing_own_edits_rather_than_creates(self):
        result = self.call()
        self.assertTrue(result['changed'])
        self.assertEqual(result['comment_id'], 55)
        self.assertEqual([x[0] for x in self.writes()], ['PATCH'])
        self.assertEqual(self.client.rows[1]['body'], 'old')
        self.assertEqual(self.client.calls[-1][1], 'repos/o/r/issues/comments/55')

    def test_newest_creation_not_edit_or_catalog_order_and_id_tie(self):
        older = self.client.row(100, 3, '2026-10-19T10:00:00Z');older['updated_at'] = '2099-01-01'
        self.client.rows = [older, self.client.row(60, 3), self.client.row(59, 3)]
        self.assertEqual(self.call()['comment_id'], 60)

    def test_noop_rechecks_catalog_and_parent_without_write(self):
        result = self.call(text='old')
        self.assertFalse(result['changed'])
        self.assertEqual(self.writes(), [])
        self.assertEqual(sum(c[1].endswith('/9/comments') for c in self.client.calls), 2)
        self.assertEqual(sum(c[1].endswith('/pulls/9') for c in self.client.calls), 2)

    def test_edit_without_fallback_refuses_absent_own(self):
        args = parser().parse_args(['pr','comment','9','-R','o/r','--edit-last','--body','new'])
        args.number = 9;self.client.rows = []
        with self.assertRaises(GVError) as caught: discussion_comment(self.client,'o/r',args)
        self.assertEqual(caught.exception.code, 'comment_not_found')
        self.assertFalse(caught.exception.details['mutation_attempted'])
        self.assertEqual(self.writes(), [])

    def test_invalid_combinations_before_client_or_selection(self):
        for selector in ('9', 'branch'):
            for flags in (['--create-if-none'], ['--edit-last','--create-if-none','--reply-to','3'],
                          ['--edit-last','--create-if-none','--path','a','--line','1'],
                          ['--edit-last','--create-if-none','--attach','missing.png'],
                          ['--edit-last','--create-if-none','--commit','a'*40]):
                args = parser().parse_args(['pr','comment',selector,'-R','o/r','--body','new','--dry-run',*flags])
                with patch('gitverse_cli.cli.Client', side_effect=AssertionError('network/client')):
                    with self.assertRaises(GVError) as caught: execute(args)
                self.assertEqual(caught.exception.code, 'usage_error')

    def test_offline_dry_run_no_network_and_no_predicted_selection(self):
        self.client.dry_run = True
        result = self.call()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(result['method'], 'select PATCH or POST')
        self.assertNotIn('comment_id', result)
        self.assertTrue(result['create_if_none'])

    def test_file_stdin_works_in_fallback_mode(self):
        args = parser().parse_args(['pr','comment','9','-R','o/r','--edit-last','--create-if-none','-F','-'])
        args.number=9;self.client.rows=[]
        with patch('sys.stdin', io.StringIO('stdin\r\n')):
            self.assertEqual(discussion_comment(self.client,'o/r',args)['body'], 'stdin\r\n')

    def test_malformed_catalog_cannot_be_absence(self):
        base = self.client.row(55,3)
        bads = [None, {}, [None], [{'id':True}], [base,base]]
        for field, value in [('user',{}),('user',{'id':True}),('issue_url','https://evil.test/x'),
                             ('issue_url',self.client.base+'/repos/o/r/issues/10'),('body',None),
                             ('updated_at',None),('created_at',None),('created_at','yesterday'),
                             ('created_at','2026-10-20T10:00:00')]:
            row=copy.deepcopy(base);row[field]=value;bads.append([row])
        for bad in bads:
            self.client=Fake();original=self.client.request
            def request(method,path,**kwargs):
                return bad if method=='GET' and path.endswith('/9/comments') else original(method,path,**kwargs)
            self.client.request=request
            with self.subTest(bad=bad):
                with self.assertRaises(GVError): self.call()
                self.assertEqual(self.writes(), [])
                self.assertTrue(all('evil.test' not in c[1] for c in self.client.calls))

    def test_catalog_entry_bound_no_partial_selection(self):
        with self.assertRaises(GVError) as caught: self.call('--max-entries','1')
        self.assertEqual(caught.exception.code, 'catalog_limit')
        self.assertEqual(self.writes(), [])

    def test_local_selection_deadline(self):
        with self.assertRaises(GVError) as caught:
            newest_own(self.client.request,'repos/o/r/issues/9/comments',self.client.base+'/repos/o/r/issues/9',3,100,time.monotonic()-1)
        self.assertEqual(caught.exception.code,'deadline_exceeded')

    def test_new_own_between_catalogs_refuses_not_create(self):
        self.client.rows=[];count=0
        def hook(client, method, path):
            nonlocal count
            if method=='GET' and path.endswith('/9/comments'):
                count+=1
                if count==2: client.rows.append(client.row(88,3))
        self.client.hook=hook
        with self.assertRaises(GVError) as caught: self.call()
        self.assertEqual(caught.exception.code, 'concurrent_change')
        self.assertFalse(caught.exception.details['mutation_attempted'])
        self.assertEqual(self.writes(), [])

    def test_selected_comment_or_parent_changes_refuse(self):
        for target in ('comment','parent'):
            self.client=Fake();count=0
            def hook(client,method,path):
                nonlocal count
                if target=='comment' and path.endswith('/comments/55'): client.rows[0]['updated_at']='v2'
                if target=='parent' and path.endswith('/pulls/9'):
                    count+=1
                    if count==2: client.parent['updated_at']='v2'
            self.client.hook=hook
            with self.assertRaises(GVError) as caught: self.call()
            self.assertEqual(caught.exception.code,'concurrent_change')
            self.assertEqual(self.writes(), [])

    def test_missing_permission_no_fallback_on_error(self):
        for status in (401,403,404):
            self.client=Fake()
            def hook(client,method,path):
                if path.endswith('/9/comments'): raise GVError('denied','http_error',status)
            self.client.hook=hook
            with self.assertRaises(GVError): self.call()
            self.assertEqual(self.writes(), [])

    def test_uncertain_write_is_single_and_retains_recovery(self):
        for mode in ('create','edit'):
            for error in (GVError('timeout','network_error'),http.client.IncompleteRead(b''),OSError('private')):
                self.client=Fake()
                if mode=='create': self.client.rows=[]
                self.client.fail=error
                with self.assertRaises(GVError) as caught: self.call()
                self.assertEqual(len(self.writes()),1)
                self.assertTrue(caught.exception.details['mutation_attempted'])
                self.assertEqual(caught.exception.details['stage'],mode)
                self.assertEqual(caught.exception.details['repository'],'o/r')
                if mode=='edit': self.assertEqual(caught.exception.details['comment_id'],55)

    def test_edit_readback_mismatch_is_unknown_not_success(self):
        self.client.ignore=True
        with self.assertRaises(GVError) as caught: self.call()
        self.assertEqual(caught.exception.code,'verification_failed')
        self.assertEqual(caught.exception.details['comment_id'],55)
        self.assertEqual(caught.exception.details['stage'],'verify')
        self.assertEqual(len(self.writes()),1)

    def test_complete_transport_bounds_and_no_credentials_in_remote_url(self):
        self.call()
        self.assertEqual(len({c[4]['deadline'] for c in self.client.calls}),1)
        self.assertTrue(all(c[4]['require_complete'] and c[4]['max_response_bytes']==33554432 for c in self.client.calls))
        self.assertTrue(all(not c[1].startswith('http') for c in self.client.calls))

    def test_incomplete_size_time_failures_never_create(self):
        for code in ('incomplete_response','response_limit','deadline_exceeded','pagination_limit'):
            self.client=Fake()
            def hook(client,method,path):
                if path.endswith('/9/comments'): raise GVError('bound',code)
            self.client.hook=hook
            with self.assertRaises(GVError) as caught: self.call()
            self.assertEqual(caught.exception.code,code)
            self.assertEqual(self.writes(),[])

    def test_web_refused_without_request(self):
        self.client.backend='web'
        with self.assertRaises(GVError): self.call()
        self.assertEqual(self.client.calls,[])

    def test_real_edit_redirect_never_forwards_credentials_or_retries(self):
        import json
        import urllib.error
        from unittest.mock import Mock
        from gitverse_cli.client import Client, NoRedirect
        class Response(io.BytesIO): status=200
        client=Client(dry_run=True);client.dry_run=False
        client.headers['Authorization']='Bearer synthetic-test-token'
        client.opener=Mock()
        client.opener.open.side_effect=[Response(json.dumps(d).encode()) for d in (
            self.client.parent, {'id':3,'login':'me'}, self.client.rows,
            self.client.rows[0], self.client.rows, self.client.parent)] + [
                urllib.error.HTTPError(client.base,307,'refused',{'Location':'https://other.invalid/x'},None)]
        args=parser().parse_args(['pr','comment','9','-R','o/r','--edit-last','--create-if-none','--body','new']);args.number=9
        with self.assertRaises(GVError) as caught: discussion_comment(client,'o/r',args)
        self.assertEqual(caught.exception.status,307)
        self.assertEqual(caught.exception.details['comment_id'],55)
        self.assertEqual(caught.exception.details['stage'],'edit')
        self.assertTrue(caught.exception.details['mutation_attempted'])
        self.assertEqual(client.opener.open.call_count,7)
        self.assertEqual([c.args[0].method for c in client.opener.open.call_args_list].count('PATCH'),1)
        for call in client.opener.open.call_args_list:
            self.assertTrue(call.args[0].full_url.startswith(client.base+'/'))
            self.assertIsNone(call.args[0].get_header('Cookie'))
        self.assertIsNone(NoRedirect().redirect_request(None,None,307,'',{},'https://other.invalid/x'))
