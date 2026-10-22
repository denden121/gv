"""Public maintainer branch permission; mocked contracts, never live proof."""
import copy
import unittest
from unittest.mock import patch

import test_pr_create as core
from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from gitverse_cli.pr_create import create_pull
from test_metadata import Public, REPO


class MaintainerEditTests(unittest.TestCase):
    def setUp(self):
        self.c = Public()
        original = self.c.request
        def request(method, path, query=None, body=None, **kwargs):
            if method == 'POST' and path.endswith('/pulls'):
                self.c.pull['maintainer_can_modify'] = body.get('maintainer_can_modify', True)
            return original(method, path, query, body, **kwargs)
        self.c.request = request

    create = core.CreateTests.create
    intercept = core.CreateTests.intercept
    framed_create = core.CreateTests.framed_create

    def args(self, *flags):
        return parser().parse_args(['pr','create','-R',REPO,'--head','feature','--base','main','--title','Created',*flags])

    def test_false_written_once_and_read_independently(self):
        result = self.create('--no-maintainer-edit')
        self.assertIs(result['maintainer_can_modify'], False)
        self.assertIs(self.c.writes()[0][3]['maintainer_can_modify'], False)
        self.assertEqual(len(self.c.writes()), 1)
        self.assertEqual(self.c.calls[-1][1], 'repos/owner/repo/pulls/8')

    def test_omission_preserves_existing_wire_contract(self):
        self.assertIs(self.create()['maintainer_can_modify'], True)
        self.assertNotIn('maintainer_can_modify', self.c.writes()[0][3])

    def test_draft_literal_body_assignee_and_label_combination(self):
        with patch('gitverse_cli.metadata.web_client', side_effect=AssertionError('cookies')):
            result = self.create('--no-maintainer-edit','--draft','--body','  Б\r\n','-a','@me','-l','one')
        self.assertIs(result['maintainer_can_modify'], False)
        self.assertIs(result['draft'], True)
        self.assertEqual(result['body'], '  Б\r\n')
        self.assertEqual(result['assignees'][0]['id'], 1)
        self.assertEqual(result['labels'][0]['id'], 11)
        self.assertEqual(len(self.c.writes()), 1)

    def test_offline_preview_no_auth_network_or_editor(self):
        for flags in [[], ['-a','@me','-l','one'], ['--editor'], ['-m','Release']]:
            with self.subTest(flags=flags), patch('gitverse_cli.client.load_auth', side_effect=AssertionError('auth')):
                c = Client(dry_run=True)
                with patch.object(c.opener,'open',side_effect=AssertionError('network')), patch('gitverse_cli.comment_editor.edit_text',side_effect=AssertionError('editor')):
                    result = create_pull(c, REPO, self.args('--dry-run','--no-maintainer-edit',*flags))
                template = result.get('base_request_template',result)
                self.assertIs(template['body']['maintainer_can_modify'],False)
                self.assertEqual(result['network_requests'],0)
                self.assertEqual(result['mutation_outcome'],'none')

    def test_bad_permission_in_post_or_readback_retains_recovery_ids(self):
        for stage in ['POST','GET']:
            for value in [True, None, 0, 'false', {}, []]:
                with self.subTest(stage=stage,value=value):
                    self.setUp()
                    def hook(m,p,r,kw):
                        if m == stage and (m == 'POST' or p.endswith('/pulls/8')):
                            r['maintainer_can_modify']=value
                        return r
                    self.intercept(hook)
                    with self.assertRaises(GVError) as caught:self.create('--no-maintainer-edit')
                    self.assertEqual(caught.exception.code,'verification_failed')
                    self.assertEqual(caught.exception.details['pull_id'],88)
                    self.assertEqual(caught.exception.details['pull_number'],8)
                    self.assertEqual(caught.exception.details['mutation_outcome'],'confirmed')
                    self.assertEqual(len(self.c.writes()),1)

    def test_missing_permission_field_is_not_false(self):
        self.intercept(lambda m,p,r,kw: ({k:v for k,v in r.items() if k!='maintainer_can_modify'} if m=='POST' else r))
        with self.assertRaises(GVError):self.create('--no-maintainer-edit')
        self.assertEqual(len(self.c.writes()),1)

    def test_concurrent_permission_change_after_metadata_read(self):
        def hook(m,p,r,kw):
            if p.endswith('/pulls/8') and self.c.reads >= 2:r['maintainer_can_modify']=True
            return r
        self.intercept(hook)
        with self.assertRaises(GVError) as caught:self.create('--no-maintainer-edit','-a','@me')
        self.assertEqual(caught.exception.details['stage'],'verify_create')
        self.assertEqual(caught.exception.details['pull_id'],88)
        self.assertEqual(len(self.c.writes()),1)

    def test_wrong_scope_or_identity_refuses_prewrite(self):
        for target,value in [('repos/owner/repo',{'id':7,'full_name':'other/repo','private':True}),('user',{'id':True,'login':'me'})]:
            with self.subTest(target=target):
                self.setUp();self.intercept(lambda m,p,r,kw:value if p==target else r)
                with self.assertRaises(GVError) as caught:self.create('--no-maintainer-edit')
                self.assertEqual(caught.exception.details['mutation_outcome'],'none')
                self.assertEqual(self.c.writes(),[])

    def test_denied_preflight_is_not_positive_permission_proof(self):
        for status in [401,403,404]:
            self.setUp()
            self.c.request=lambda *a,**kw:(_ for _ in ()).throw(GVError('denied','http_error',status))
            with self.assertRaises(GVError) as caught:self.create('--no-maintainer-edit')
            self.assertFalse(caught.exception.details['mutation_attempted'])

    def test_unknown_post_no_retry(self):
        def hook(m,p,r,kw):
            if m=='POST':raise GVError('unknown','network_error')
            return r
        self.intercept(hook)
        with self.assertRaises(GVError) as caught:self.create('--no-maintainer-edit')
        self.assertEqual(caught.exception.details['mutation_outcome'],'unknown')
        self.assertIsNone(caught.exception.details['pull_id'])
        self.assertEqual(len(self.c.writes()),1)

    def test_incomplete_post_and_readback_never_retry(self):
        for stage in ['create','verify_create']:
            self.setUp();self.framed_create(stage)
            with self.assertRaises(GVError) as caught:self.create('--no-maintainer-edit')
            self.assertEqual(caught.exception.code,'incomplete_response')
            self.assertEqual(caught.exception.details['mutation_outcome'],'unknown' if stage=='create' else 'confirmed')
            self.assertEqual(len(self.c.writes()),1)

    def test_shared_transport_bounds(self):
        options=[]
        self.intercept(lambda m,p,r,kw:options.append(kw) or r)
        self.create('--no-maintainer-edit')
        self.assertEqual(len({kw['deadline'] for kw in options}),1)
        self.assertTrue(all(kw['require_complete'] and kw['max_response_bytes']==33554432 for kw in options))

    def test_deadline_before_post_no_write(self):
        from gitverse_cli.pr_create import Bounded
        original=Bounded.request
        def request(session,method,path,query=None,body=None):
            if method=='GET':raise GVError('deadline','deadline_exceeded')
            return original(session,method,path,query,body)
        with patch.object(Bounded,'request',request), self.assertRaises(GVError) as caught:self.create('--no-maintainer-edit')
        self.assertEqual(caught.exception.details['mutation_outcome'],'none')
        self.assertEqual(self.c.writes(),[])

    def test_redirect_never_forwards_credentials(self):
        from unittest.mock import Mock
        from urllib.error import HTTPError
        c=Client(dry_run=True);c.dry_run=False;c.opener=Mock()
        c.opener.open.side_effect=HTTPError('https://gitverse.ru/api/v1/repos/owner/repo/pulls',307,'redirect',{'Location':'https://other.invalid/capture'},None)
        with self.assertRaises(GVError):c.request('POST','repos/owner/repo/pulls',body={'maintainer_can_modify':False})
        c.opener.open.assert_called_once()

    def test_discovery(self):
        info=execute(parser().parse_args(['agent-info']))
        self.assertEqual(info['pr_create_no_maintainer_edit']['flags'],['--no-maintainer-edit'])
