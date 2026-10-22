"""Cross-owner source contracts. Synthetic clients do not establish live parity."""
import copy
import http.client
import os
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.pr_create_head import Resolver
from test_metadata import Public, REPO, USERS


class ForkPublic(Public):
    def __init__(self):
        super().__init__()
        self.base = {'id': 7, 'full_name': REPO, 'private': True, 'fork': False,
                     'owner': {'id': 3, 'login': 'owner', 'type': 'User'}}
        self.source = {'id': 9, 'full_name': 'me/renamed', 'private': True, 'fork': True,
                       'parent': copy.deepcopy(self.base), 'owner': {'id': 1, 'login': 'me', 'type': 'User'}}
        self.repos = [self.source]
        self.heads = [{'name': 'feature/slash', 'commit': {'sha': 'a'*40}}]
        self.bases = [{'name': 'main', 'commit': {'sha': 'b'*40}}]
        self.tweak = None

    def request(self, m, p, q=None, body=None, **kw):
        if self.tweak:
            self.tweak(m, p, q, body)
        if p == 'user/repos' or p == 'users/me/repos':
            row = self.repos if q['page'] == 1 else []
        elif p == 'users/me':
            row = {**USERS['me'], 'type': 'User'}
        elif p == 'repos/' + REPO:
            row = self.base
        elif p == 'repos/me/renamed':
            row = self.source
        elif p == 'repos/me/renamed/branches':
            row = self.heads if q['page'] == 1 else []
        elif p == 'repos/' + REPO + '/branches':
            row = self.bases if q['page'] == 1 else []
        else:
            if m == 'POST':
                self.pull['head'] = {'ref': body['head'], 'sha': 'a'*40,
                                     'repo': {'id': 9, 'full_name': 'me/renamed'}, 'repo_id': 9}
                self.pull['base']['ref'] = body['base']
            return super().request(m, p, q, body, **kw)
        self.calls.append((m,p,copy.deepcopy(q),copy.deepcopy(body)))
        return copy.deepcopy(row)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.c = ForkPublic()
        env = patch.dict(os.environ, {}, clear=True); env.start(); self.addCleanup(env.stop)

    def create(self, *extra):
        args = parser().parse_args(['pr', 'create', '-R', REPO, '--head', 'me:feature/slash',
                                   '--base', 'main', '--title', 'Created', *extra])
        with patch('gitverse_cli.cli.Client', return_value=self.c):
            return execute(args)

    def resolve(self):
        return Resolver(self.c).resolve(REPO, self.c.base, 'me', USERS['me'], 'feature/slash', 'main')

    def test_renamed_private_owned_fork_and_exact_readback(self):
        result = self.create('--draft', '--body', '', '--assignee', '@me', '--label', 'one')
        self.assertEqual(result['head']['repo']['id'], 9)
        self.assertEqual(result['base']['repo']['id'], 7)
        self.assertTrue(result['create_verification']['readback'])
        payload = self.c.writes()[0][3]
        self.assertEqual(payload['head'], 'feature/slash')
        self.assertEqual(payload['head_repo'], 'renamed')
        self.assertEqual(payload['assignees'], ['me'])
        self.assertEqual(len(self.c.writes()), 1)
        self.assertEqual(sum(p == 'user/repos' and q['page'] == 1 for _,p,q,_ in self.c.calls), 2)

    def test_same_branch_name_across_distinct_repositories_is_valid(self):
        self.c.bases[0]['name'] = 'feature/slash'
        self.assertEqual(self.create('--base', 'feature/slash')['head']['ref'], 'feature/slash')

    def test_other_user_catalog_is_explicitly_public_scope(self):
        author = {'id': 55, 'login': 'different'}
        result = Resolver(self.c).resolve(REPO,self.c.base,'me',author,'feature/slash','main')
        self.assertEqual(result['id'],9)
        self.assertTrue(any(p == 'users/me/repos' for _,p,_,_ in self.c.calls))

    def test_no_visible_fork_or_branch_is_not_platform_absence(self):
        for field in ('repos', 'heads', 'bases'):
            with self.subTest(field=field):
                self.c = ForkPublic(); setattr(self.c,field,[])
                with self.assertRaises(GVError) as caught: self.create()
                self.assertEqual(caught.exception.code,'head_not_visible')
                self.assertEqual(self.c.writes(),[])

    def test_multiple_matching_forks_refuse(self):
        second = copy.deepcopy(self.c.source); second.update(id=10, full_name='me/second')
        self.c.repos.append(second)
        original=self.c.request
        self.c.request=lambda m,p,q=None,body=None,**kw: copy.deepcopy(second) if p=='repos/me/second' else original(m,p.replace('repos/me/second/','repos/me/renamed/'),q,body,**kw)
        with self.assertRaises(GVError) as caught: self.create()
        self.assertEqual(caught.exception.code,'ambiguous_head')
        self.assertEqual(self.c.writes(),[])

    def test_malformed_fork_owner_scope_parent_and_sha_refuse(self):
        for key,value in (('id',True),('full_name',None),('full_name',{}),('full_name','foreign/renamed'),('owner',{'id':88,'login':'me','type':'User'}),('fork',None),('parent',None)):
            self.c=ForkPublic(); self.c.source[key]=value
            with self.subTest(key=key), self.assertRaises(GVError):self.create()
            self.assertEqual(self.c.writes(),[])
        for rows in ([None],[{'name':'feature/slash','commit':None}],[{'name':'feature/slash','commit':{'sha':'bad'}}]):
            self.c=ForkPublic();self.c.heads=rows
            with self.assertRaises(GVError):self.create()
            self.assertEqual(self.c.writes(),[])

    def test_organization_and_missing_profile_type_refuse(self):
        for type_value in ('Organization',None):
            self.c=ForkPublic(); original=self.c.request
            self.c.request=lambda m,p,q=None,body=None,**kw: {'id':1,'login':'me','type':type_value} if p=='users/me' else original(m,p,q,body,**kw)
            with self.assertRaises(GVError):self.create()
            self.assertEqual(self.c.writes(),[])

    def test_unrelated_fork_and_cycle_do_not_become_source(self):
        self.c.source['parent'].update(id=90,full_name='other/root')
        original=self.c.request
        self.c.request=lambda m,p,q=None,body=None,**kw: {'id':90,'full_name':'other/root','private':True,'fork':False} if p=='repos/other/root' else original(m,p,q,body,**kw)
        with self.assertRaises(GVError) as caught:self.create()
        self.assertEqual(caught.exception.code,'head_not_visible')
        self.c=ForkPublic();self.c.source['parent']=copy.deepcopy(self.c.source)
        with self.assertRaises(GVError):self.create()
        self.assertEqual(self.c.writes(),[])

    def test_catalog_duplicate_shift_and_empty_termination(self):
        self.c.repos.append(copy.deepcopy(self.c.source))
        with self.assertRaises(GVError) as caught:self.create()
        self.assertEqual(caught.exception.code,'pagination_error')
        self.c=ForkPublic();self.resolve()
        self.assertTrue(any(p=='user/repos' and q['page']==2 for _,p,q,_ in self.c.calls))
        self.assertTrue(any(p.endswith('/branches') and q['page']==2 for _,p,q,_ in self.c.calls))

    def test_page_entry_request_and_ancestry_bounds_refuse(self):
        for bound,value in (('MAX_PAGES',1),('MAX_ENTRIES',1),('MAX_REQUESTS',1),('MAX_DEPTH',1)):
            self.c=ForkPublic()
            with self.subTest(bound=bound), patch('gitverse_cli.pr_create_head.'+bound,value), self.assertRaises(GVError):self.create()
            self.assertEqual(self.c.writes(),[])

    def test_oversized_repository_or_branch_page_refuses_before_selection(self):
        for field in ('repos', 'heads', 'bases'):
            self.c = ForkPublic()
            # Even a valid match first must not permit a partial selection from
            # a server response that violates the requested page bound.
            rows = getattr(self.c, field)
            rows.extend(copy.deepcopy(rows[0]) for _ in range(100))
            with self.subTest(catalog=field), self.assertRaises(GVError) as caught:
                self.create()
            self.assertEqual(caught.exception.code, 'pagination_limit')
            self.assertFalse(caught.exception.details['mutation_attempted'])
            self.assertEqual(self.c.writes(), [])

    def test_exact_page_size_continues_to_empty_page(self):
        self.c.heads = [
            {'name': 'feature/slash' if n == 0 else 'other-' + str(n),
             'commit': {'sha': 'a' * 40}}
            for n in range(100)
        ]
        self.assertEqual(self.resolve()['id'], 9)
        self.assertTrue(any(p == 'repos/me/renamed/branches' and q['page'] == 2
                            for _, p, q, _ in self.c.calls))

    def test_permission_and_transport_limits_never_write(self):
        for code,status in (('http_error',401),('http_error',403),('http_error',404),('deadline_exceeded',None),('response_limit',None)):
            self.c=ForkPublic()
            def fail(m,p,q,b):
                if p=='user/repos':raise GVError('refused',code,status)
            self.c.tweak=fail
            with self.assertRaises(GVError) as caught:self.create()
            self.assertFalse(caught.exception.details['mutation_attempted'])
            self.assertEqual(self.c.writes(),[])

    def test_changed_source_or_new_ambiguity_before_post_refuses(self):
        for change in ('sha','missing'):
            self.c=ForkPublic();count=0
            def tweak(m,p,q,b):
                nonlocal count
                if p=='user/repos' and q['page']==1:
                    count+=1
                    if count==2:
                        if change=='sha':self.c.heads[0]['commit']['sha']='c'*40
                        else:self.c.repos=[]
            self.c.tweak=tweak
            with self.assertRaises(GVError):self.create()
            self.assertEqual(self.c.writes(),[])

    def test_wrong_head_base_scope_or_changed_sha_retains_recovery_ids(self):
        for side,key,value in (('head','repo',{'id':7,'full_name':REPO}),('base','repo',{'id':9,'full_name':'me/renamed'}),('head','sha','c'*40),('head','repo_id',7)):
            self.c=ForkPublic()
            def on_read(c,n):c.pull[side][key]=value
            self.c.on_read=on_read
            with self.assertRaises(GVError) as caught:self.create()
            details=caught.exception.details
            self.assertEqual((details['pull_number'],details['pull_id']),(8,88))
            self.assertEqual(details['source']['id'],9)
            self.assertEqual(details['mutation_outcome'],'confirmed')
            self.assertEqual(len(self.c.writes()),1)

    def test_interrupted_post_is_unknown_non_retried_and_keeps_source(self):
        def fail(m,p,q,b):
            if m=='POST':raise http.client.IncompleteRead(b'')
        self.c.tweak=fail
        with self.assertRaises(GVError) as caught:self.create()
        self.assertEqual(caught.exception.code,'network_error')
        self.assertEqual(caught.exception.details['mutation_outcome'],'unknown')
        self.assertEqual(caught.exception.details['source']['repository'],'me/renamed')
        self.assertEqual(caught.exception.details['stage'],'create')

    def test_equal_inputs_still_create_each_time(self):
        self.create();self.create()
        self.assertEqual(len(self.c.writes()),2)

    def test_third_party_source_never_uses_ambiguous_short_name_for_write(self):
        original=self.c.request
        self.c.request=lambda m,p,q=None,body=None,**kw: {'id':55,'login':'different'} if p=='user' else original(m,p,q,body,**kw)
        with self.assertRaises(GVError) as caught:self.create()
        self.assertEqual(caught.exception.code,'feature_unavailable')
        self.assertEqual(caught.exception.details['stage'],'source_wire_contract')
        self.assertEqual(caught.exception.details['source']['id'],9)
        self.assertEqual(self.c.writes(),[])

    def test_fill_uses_distinct_source_base_sha_pins(self):
        from gitverse_cli.pr_create_fill import verify_remote
        context={'pins':{'base':'b'*40,'head':'a'*40}}
        source=self.resolve()
        verify_remote(self.c,REPO,'main','feature/slash',context,source)
        context['pins']['head']='c'*40
        with self.assertRaises(GVError):verify_remote(self.c,REPO,'main','feature/slash',context,source)

    def test_foreign_origin_parent_is_rejected_without_request_to_origin(self):
        self.c.source['parent']['full_name']='https://foreign.invalid/owner/repo'
        with self.assertRaises(GVError):self.create()
        self.assertFalse(any('foreign.invalid' in p for _,p,_,_ in self.c.calls))

    def test_current_identity_changed_after_source_scan_never_writes(self):
        original=self.c.request; count=0
        def request(m,p,q=None,body=None,**kw):
            nonlocal count
            if p=='user':
                count+=1
                if count==2:return {'id':55,'login':'different'}
            return original(m,p,q,body,**kw)
        self.c.request=request
        with self.assertRaises(GVError) as caught:self.create()
        self.assertEqual(caught.exception.code,'concurrent_change')
        self.assertEqual(self.c.writes(),[])

    def test_authenticated_catalog_filters_accessible_organization_repositories(self):
        self.c.repos.insert(0, {'id':90,'full_name':'organization/visible','private':True,'fork':False,
                               'owner':{'id':55,'login':'organization','type':'Organization'}})
        self.assertEqual(self.create()['head']['repo']['id'],9)
