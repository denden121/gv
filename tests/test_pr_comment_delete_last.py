import copy
import http.client
import io
import json
import time
import unittest
from unittest.mock import Mock, patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError, NoRedirect
from gitverse_cli.comment_delete import delete_last
from test_pr_comment import Fake


class DeleteFake(Fake):
    def request(self, method, path, query=None, body=None, **kwargs):
        if method == 'DELETE':
            self.calls.append((method,path,query,body,kwargs))
            if self.fail: raise self.fail
            if self.hook: self.hook(self,method,path)
            if not self.ignore: self.rows = [r for r in self.rows if r['id'] != int(path.split('/')[-1])]
            return None
        if method == 'GET' and '/issues/comments/' in path and not any(r['id']==int(path.split('/')[-1]) for r in self.rows):
            self.calls.append((method,path,query,body,kwargs))
            if self.hook: self.hook(self,method,path)
            raise GVError('missing','http_error',404)
        return super().request(method,path,query,body,**kwargs)


class DeleteLastTests(unittest.TestCase):
    def setUp(self): self.client=DeleteFake()
    def args(self,*flags):
        args=parser().parse_args(['pr','comment','9','-R','o/r','--delete-last',*flags]);args.number=9
        return args
    def call(self,*flags): return delete_last(self.client,'o/r',self.args('--yes',*flags))
    def writes(self): return [c for c in self.client.calls if c[0]!='GET']

    def test_positive_one_delete_204_then_independent_404(self):
        result=self.call()
        self.assertTrue(result['deleted']);self.assertEqual(result['comment_id'],55)
        self.assertEqual([r['id'] for r in self.client.rows],[56])
        self.assertEqual([c[0] for c in self.writes()],['DELETE'])
        self.assertEqual(self.writes()[0][4]['expected_status'],204)
        self.assertEqual(self.client.calls[-1][1],'repos/o/r/issues/comments/55')
        self.assertIn('replies',result['cascade'])
        self.assertEqual(sum(c[1].endswith('/9/comments') for c in self.client.calls),2)

    def test_newest_created_time_then_id_not_update_or_order(self):
        old=self.client.row(100,3,'2026-10-19T10:00:00Z');old['updated_at']='2099'
        self.client.rows=[old,self.client.row(60,3),self.client.row(59,3),self.client.row(200,4)]
        self.assertEqual(self.call()['comment_id'],60)
        self.assertEqual({r['id'] for r in self.client.rows},{100,59,200})

    def test_absence_and_other_authors_refuse_without_delete(self):
        for rows in ([],[self.client.row(56,4)]):
            self.client=DeleteFake();self.client.rows=rows
            with self.assertRaises(GVError) as caught:self.call()
            self.assertEqual(caught.exception.code,'comment_not_found');self.assertEqual(self.writes(),[])
            self.assertFalse(caught.exception.details['mutation_attempted'])

    def test_yes_required_and_conflicts_before_selection(self):
        bads=[[],['--yes','--body','x'],['--yes','-F','missing'],['--yes','--edit-last'],
              ['--yes','--create-if-none'],['--yes','--attach','missing'],['--yes','--path','a','--line','1'],
              ['--yes','--reply-to','3'],['--yes','--commit','a'*40]]
        for flags in bads:
            with patch('gitverse_cli.cli.Client',side_effect=AssertionError('client/network')):
                with self.assertRaises(GVError) as caught:execute(self.args(*flags))
            self.assertEqual(caught.exception.code,'usage_error')
        args=parser().parse_args(['pr','comment','branch','-R','o/r','--yes','--body','x'])
        with patch('gitverse_cli.cli.Client',side_effect=AssertionError('client/network')):
            with self.assertRaises(GVError):execute(args)

    def test_offline_dry_run_without_yes_no_selection_or_network(self):
        self.client.dry_run=True
        result=delete_last(self.client,'o/r',self.args('--dry-run'))
        self.assertEqual(self.client.calls,[]);self.assertNotIn('comment_id',result)
        self.assertEqual(result['method'],'select DELETE');self.assertIn('replies',result['cascade'])

    def test_malformed_or_wrong_scope_catalog_refuses(self):
        base=self.client.row(55,3)
        bads=[None,{},[None],[{'id':True}],[base,base]]
        for field,value in [('user',{}),('user',{'id':True}),('issue_url','https://other.invalid/x'),
                            ('issue_url',self.client.base+'/repos/o/r/issues/10'),('body',None),
                            ('updated_at',None),('created_at',None),('created_at','yesterday'),
                            ('created_at','2026-10-20T10:00:00')]:
            row=copy.deepcopy(base);row[field]=value;bads.append([row])
        for bad in bads:
            self.client=DeleteFake();original=self.client.request
            def request(method,path,**kwargs):
                return bad if path.endswith('/9/comments') else original(method,path,**kwargs)
            self.client.request=request
            with self.assertRaises(GVError):self.call()
            self.assertEqual(self.writes(),[])

    def test_wrong_user_and_parent_identity_refuse(self):
        for target in ('user','parent','comment'):
            self.client=DeleteFake();original=self.client.request
            def request(method,path,**kwargs):
                row=original(method,path,**kwargs)
                if target=='user' and path=='user':return {'id':True,'login':'bad'}
                if target=='parent' and '/pulls/' in path:row['base']['repo']['full_name']='other/repo'
                if target=='comment' and '/issues/comments/' in path:row['user']['id']=4
                return row
            self.client.request=request
            with self.assertRaises(GVError):self.call()
            self.assertEqual(self.writes(),[])

    def test_concurrent_selected_newest_and_parent_changes(self):
        for target in ('comment','newest','parent'):
            self.client=DeleteFake();count=0
            def hook(c,m,p):
                nonlocal count
                if target=='comment' and '/issues/comments/' in p:c.rows[0]['updated_at']='v2'
                if target=='newest' and p.endswith('/9/comments'):
                    count+=1
                    if count==2:c.rows.append(c.row(100,3))
                if target=='parent' and '/pulls/' in p:
                    count+=1
                    if count==2:c.parent['updated_at']='v2'
            self.client.hook=hook
            with self.assertRaises(GVError) as caught:self.call()
            self.assertEqual(caught.exception.code,'concurrent_change');self.assertEqual(self.writes(),[])

    def test_permissions_and_incomplete_bounds_never_mean_absence(self):
        failures=[GVError('denied','http_error',s) for s in (401,403,404)]
        failures += [GVError('limit',code) for code in ('incomplete_response','response_limit','deadline_exceeded','pagination_limit')]
        for failure in failures:
            self.client=DeleteFake()
            def hook(c,m,p):
                if p.endswith('/9/comments'):raise failure
            self.client.hook=hook
            with self.assertRaises(GVError) as caught:self.call()
            self.assertEqual(caught.exception.code,failure.code);self.assertEqual(self.writes(),[])
        self.client=DeleteFake()
        with self.assertRaises(GVError) as caught:self.call('--max-entries','1')
        self.assertEqual(caught.exception.code,'catalog_limit');self.assertEqual(self.writes(),[])

    def test_transport_deadline_and_complete_bounds(self):
        self.call()
        self.assertEqual(len({c[4]['deadline'] for c in self.client.calls}),1)
        self.assertTrue(all(c[4]['require_complete'] and c[4]['max_response_bytes']==33554432 for c in self.client.calls))
        self.assertTrue(all(not c[1].startswith('http') for c in self.client.calls))
        self.client=DeleteFake()
        with patch('gitverse_cli.pr_comment.time.monotonic',side_effect=[10,100]):
            with self.assertRaises(GVError):self.call()
        self.assertEqual(self.writes(),[])

    def test_delete_unknown_outcomes_no_retry_recovery_id(self):
        for failure in (GVError('timeout','network_error'),OSError('private'),http.client.IncompleteRead(b'private')):
            self.client=DeleteFake();self.client.fail=failure
            with self.assertRaises(GVError) as caught:self.call()
            error=caught.exception
            self.assertEqual(len(self.writes()),1);self.assertEqual(error.details['comment_id'],55)
            self.assertTrue(error.details['mutation_attempted']);self.assertEqual(error.details['stage'],'delete')
            self.assertNotIn('private',json.dumps(error.as_dict()))

    def test_readback_visible_denied_or_transport_is_not_success(self):
        self.client.ignore=True
        with self.assertRaises(GVError) as caught:self.call()
        self.assertEqual(caught.exception.code,'deletion_not_confirmed')
        for failure in (GVError('denied','http_error',403),OSError('private'),GVError('bad','unexpected_response')):
            self.client=DeleteFake()
            def hook(c,m,p):
                if m=='GET' and '/issues/comments/' in p and not any(r['id']==55 for r in c.rows):raise failure
            self.client.hook=hook
            with self.assertRaises(GVError) as caught:self.call()
            self.assertEqual(caught.exception.details['stage'],'verify')
            self.assertTrue(caught.exception.details['deletion_accepted']);self.assertEqual(len(self.writes()),1)

    def test_nonempty_delete_response_unknown(self):
        original=self.client.request
        def request(method,path,**kwargs):
            row=original(method,path,**kwargs)
            return {} if method=='DELETE' else row
        self.client.request=request
        with self.assertRaises(GVError) as caught:self.call()
        self.assertEqual(caught.exception.code,'unexpected_response');self.assertEqual(len(self.writes()),1)

    def test_web_refuses_without_request(self):
        self.client.backend='web'
        with self.assertRaises(GVError):self.call()
        self.assertEqual(self.client.calls,[])

    def test_real_delete_redirect_no_credential_forward_or_retry(self):
        import urllib.error
        class Response(io.BytesIO):status=200
        client=Client(dry_run=True);client.dry_run=False;client.opener=Mock()
        client.headers['Authorization']='Bearer synthetic-test-token'
        client.opener.open.side_effect=[Response(json.dumps(row).encode()) for row in (
            self.client.parent,{'id':3,'login':'me'},self.client.rows,self.client.rows[0],self.client.rows,self.client.parent)]+[
                urllib.error.HTTPError(client.base,307,'refused',{'Location':'https://other.invalid/x'},None)]
        with self.assertRaises(GVError) as caught:delete_last(client,'o/r',self.args('--yes'))
        self.assertEqual(caught.exception.status,307);self.assertTrue(caught.exception.details['mutation_attempted'])
        self.assertEqual(client.opener.open.call_count,7)
        self.assertEqual(sum(c.args[0].method=='DELETE' for c in client.opener.open.call_args_list),1)
        for call in client.opener.open.call_args_list:
            self.assertTrue(call.args[0].full_url.startswith(client.base+'/'))
            self.assertIsNone(call.args[0].get_header('Cookie'))
        self.assertIsNone(NoRedirect().redirect_request(None,None,307,'',{},'https://other.invalid/x'))
