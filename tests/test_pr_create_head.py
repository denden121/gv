"""Head selection contracts; mocks are never live evidence."""
import copy
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from gitverse_cli.pr_create import branch, select_head
from test_metadata import Public, REPO


class HeadTests(unittest.TestCase):
    def setUp(self):
        self.c = Public()
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)

    def create(self, *flags):
        args = parser().parse_args(['pr', 'create', '-R', REPO, '--base', 'main', '--title', 'Created', *flags])
        with patch('gitverse_cli.cli.Client', return_value=self.c):
            return execute(args)

    def test_explicit_head_and_alias_never_read_checkout(self):
        for flag in ('--head', '-H'):
            self.c = Public()
            with patch('gitverse_cli.pr_create_fill.git_read', side_effect=AssertionError('local Git')):
                self.assertEqual(self.create(flag, 'feature')['head']['ref'], 'feature')
            self.assertEqual(len(self.c.writes()), 1)

    def test_empty_explicit_head_refuses_without_fallback_or_network(self):
        with patch('gitverse_cli.pr_create_fill.git_read', side_effect=AssertionError('local Git')):
            with self.assertRaises(GVError) as caught: self.create('--head', '')
        self.assertEqual(caught.exception.code, 'usage_error')
        self.assertEqual(self.c.calls, [])

    def test_default_current_branch_uses_shared_deadline(self):
        before = time.monotonic()
        with patch('gitverse_cli.pr_create_fill.git_read', return_value='feature\n') as read:
            self.assertTrue(self.create()['create_verification']['readback'])
        argv, deadline = read.call_args.args
        self.assertEqual(argv, ['symbolic-ref', '--quiet', '--short', 'HEAD'])
        self.assertGreater(deadline, before)
        self.assertLessEqual(deadline, before + 60.1)

    def test_detached_unborn_missing_or_limited_git_never_writes(self):
        for code in ('git_error', 'deadline_exceeded', 'input_limit'):
            self.c = Public()
            with patch('gitverse_cli.pr_create_fill.git_read', side_effect=GVError('refused', code)):
                with self.assertRaises(GVError) as caught: self.create()
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(self.c.calls, [])

    def test_actual_current_and_detached_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            for argv in (['init', '-q', '-b', 'head/unique'], ['-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-qm', 'Synthetic', '--allow-empty']):
                subprocess.run(['git', *argv], cwd=tmp, check=True, capture_output=True)
            old = Path.cwd()
            try:
                os.chdir(tmp)
                self.assertEqual(select_head(None, REPO, time.monotonic()+10), ('head/unique', None))
                subprocess.run(['git', 'checkout', '-q', '--detach'], check=True, capture_output=True)
                with self.assertRaises(GVError): select_head(None, REPO, time.monotonic()+10)
            finally: os.chdir(old)

    def test_exact_slash_unicode_and_case_preserved_offline(self):
        self.c = Client(dry_run=True)
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')):
            for name in ('Feature/Exact', 'ветка/тест', 'refs/heads/literal'):
                result = self.create('--head', name, '--dry-run')
                self.assertEqual(result['body']['head'], name)
                self.assertEqual(result['network_requests'], 0)
                self.assertFalse(result['git_push'])

    def test_invalid_branch_inputs(self):
        for name in ('', '@', ':x', 'owner:', 'owner:repo:branch', 'https://foreign/x', 'a..b', 'x.lock/y', '.hidden/x', 'x\n', 'x\x00', 'x\\y', 'x'*1025, '\ud800'):
            self.c = Public()
            with self.subTest(name=repr(name)):
                with self.assertRaises(GVError): self.create('--head', name)
                self.assertEqual(self.c.calls, [])
        self.assertEqual(branch('x'*1024), 'x'*1024)

    def test_same_owner_qualification_validates_user_before_creation(self):
        original = self.c.request
        def request(m, p, q=None, body=None, **kw):
            if p == 'users/OWNER':
                self.c.calls.append((m,p,q,body))
                return {'id': 3, 'login': 'owner', 'type': 'User'}
            row = original(m,p,q,body,**kw)
            if p == 'repos/' + REPO:
                row['owner'] = {'id': 3, 'login': 'owner', 'type': 'User'}
            return row
        self.c.request = request
        result = self.create('-H', 'OWNER:feature', '--draft', '--body', '')
        self.assertTrue(result['draft'])
        self.assertEqual(self.c.writes()[0][3]['head'], 'feature')
        self.assertEqual(result['body'], '')

    def test_qualified_wrong_profile_or_org_or_missing_type_never_writes(self):
        for profile in ({'id':3,'login':'wrong','type':'User'}, {'id':3,'login':'owner','type':'Organization'}, {'id':3,'login':'owner'}, None, {'id':True,'login':'owner','type':'User'}):
            self.c = Public(); original = self.c.request
            self.c.request = lambda m,p,q=None,body=None,**kw: copy.deepcopy(profile) if p=='users/owner' else original(m,p,q,body,**kw)
            with self.assertRaises(GVError) as caught: self.create('--head', 'owner:feature')
            self.assertFalse(caught.exception.details['mutation_attempted'])
            self.assertEqual(self.c.writes(), [])

    def test_qualified_profile_id_must_match_repository_owner(self):
        original = self.c.request
        def request(m,p,q=None,body=None,**kw):
            if p == 'users/owner':return {'id':3,'login':'owner','type':'User'}
            row=original(m,p,q,body,**kw)
            if p == 'repos/' + REPO:row['owner']={'id':4,'login':'owner','type':'User'}
            return row
        self.c.request=request
        with self.assertRaises(GVError) as caught:self.create('--head','owner:feature')
        self.assertEqual(caught.exception.code,'unexpected_response')
        self.assertEqual(self.c.writes(),[])

    def test_cross_owner_offline_is_unresolved_without_network(self):
        self.c = Client(dry_run=True)
        with patch('gitverse_cli.client.load_auth', side_effect=AssertionError('auth')), patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')):
            result = self.create('--head', 'other:main', '--dry-run')
        self.assertFalse(result['validation']['final_request_known'])
        self.assertFalse(result['head_selection']['source_resolved'])
        self.assertNotIn('head_repo', result['body'])

    def test_qualified_offline_reports_unverified_owner_without_auth(self):
        self.c = Client(dry_run=True)
        with patch('gitverse_cli.client.load_auth', side_effect=AssertionError('auth')), patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')):
            result = self.create('-H', 'owner:feature', '--dry-run')
        self.assertFalse(result['head_selection']['owner_type_verified'])
        self.assertEqual(result['body']['head'], 'feature')
        self.assertFalse(result['validation']['remote_eligibility'])

    def test_head_equals_base_refuses_without_request(self):
        with self.assertRaises(GVError): self.create('--head', 'main')
        self.assertEqual(self.c.calls, [])

    def test_permission_and_missing_head_errors_single_attempt_recover_scope(self):
        for status in (401, 403, 404):
            self.c = Public(); original = self.c.request
            def request(m,p,q=None,body=None,**kw):
                if m=='POST':
                    self.c.calls.append((m,p,q,body)); raise GVError('refused', 'http_error', status)
                return original(m,p,q,body,**kw)
            self.c.request = request
            with self.assertRaises(GVError) as caught: self.create('-H','feature')
            self.assertEqual(caught.exception.details['head'], 'feature')
            self.assertEqual(len(self.c.writes()), 1)

    def test_selected_head_wrong_dto_and_concurrent_change_keep_ids(self):
        for field, value in (('ref','other'), ('repo',{'id':99,'full_name':REPO}), ('sha',None), ('sha','c'*40)):
            self.c = Public(); original = self.c.request
            def request(m,p,q=None,body=None,**kw):
                row = original(m,p,q,body,**kw)
                if p.endswith('/pulls/8'):row['head'][field]=value
                return row
            self.c.request = request
            with self.assertRaises(GVError) as caught:self.create('--head','feature')
            self.assertEqual(caught.exception.details['pull_id'],88)
            self.assertEqual(caught.exception.details['pull_number'],8)
            self.assertEqual(len(self.c.writes()),1)

    def test_equal_inputs_are_distinct_create_attempts_not_noop(self):
        self.create('--head','feature'); self.create('--head','feature')
        self.assertEqual(len(self.c.writes()),2)
