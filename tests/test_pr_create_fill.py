"""Real local Git histories with synthetic bounded public transport."""
import io
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import unquote

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from gitverse_cli import pr_create_fill as fill
from test_metadata import Public, REPO
import test_pr_create as core


class CreateFillTests(unittest.TestCase):
    intercept = core.CreateTests.intercept

    def setUp(self):
        self.env = patch.dict(os.environ, {'GIT_CONFIG_NOSYSTEM':'1', 'GIT_CONFIG_GLOBAL':os.devnull}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        cwd = os.getcwd(); os.chdir(self.directory.name); self.addCleanup(os.chdir, cwd)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Synthetic'); self.git('config', 'user.email', 'synthetic@example.invalid')
        self.git('commit', '-q', '--allow-empty', '-m', 'Root')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.git('remote', 'add', 'origin', 'https://gitverse.ru/'+REPO+'.git')
        self.git('update-ref', 'refs/remotes/origin/main', self.base)
        self.git('checkout', '-q', '-b', 'feature')
        self.commit('Subject 😀', 'Body\n\nLiteral $HOME')
        self.reset_client()

    def git(self, *args):
        return subprocess.run(['git', *args], capture_output=True, text=True, check=True).stdout

    def commit(self, subject, body=None):
        args = ['commit', '-q', '--allow-empty', '-m', subject]
        if body is not None: args += ['-m', body]
        self.git(*args)
        self.head = self.git('rev-parse', 'HEAD').strip()

    def reset_client(self):
        self.c = Public()
        self.c.pull['base']['sha'] = self.base
        self.c.pull['head']['sha'] = self.head
        original = self.c.request
        def request(method, path, query=None, body=None, **kwargs):
            if '/branches/' in path:
                self.c.calls.append((method, path, query, body))
                name = unquote(path.split('/branches/')[1])
                return {'name':name, 'commit':{'sha':self.base if name=='main' else self.head}}
            return original(method, path, query, body, **kwargs)
        self.c.request = request

    def call(self, *flags):
        args = parser().parse_args(['pr', 'create', '-R', REPO, '--head', 'feature', '--base', 'main', '--fill', *flags])
        with patch('gitverse_cli.cli.Client', return_value=self.c):
            return execute(args)

    def test_single_commit_subject_body_and_independent_readback(self):
        row = self.call()
        self.assertEqual((row['title'],row['body']), ('Subject 😀','Body\n\nLiteral $HOME\n'))
        self.assertEqual(row['fill']['pins'], {'base':self.base, 'head':self.head})
        self.assertTrue(row['fill']['remote_verified'])
        self.assertTrue(row['create_verification']['readback'])
        self.assertEqual(len(self.c.writes()),1)
        self.assertTrue(self.c.calls[-1][1].endswith('/pulls/8'))

    def test_multi_commit_branch_humanization_oldest_first_subjects_only(self):
        self.git('branch', '-m', 'feature', 'fix/a_b-c')
        self.commit('Second **literal**', 'Do not include verbose body')
        self.reset_client(); self.c.pull['head']['ref']='fix/a_b-c'
        row = self.call('--head','fix/a_b-c')
        self.assertEqual(row['title'],'fix/a b c')
        self.assertEqual(row['body'],'- **Subject 😀**\n- **Second **literal****\n')
        self.assertEqual(row['fill']['commits'],2)

    def test_merge_commits_are_excluded_and_diverged_base_is_not_a_subject(self):
        self.git('checkout','-q','-b','side',self.base)
        self.commit('Side subject')
        self.git('checkout','-q','feature')
        self.git('merge','--no-ff','-m','Do not include merge','side')
        self.head=self.git('rev-parse','HEAD').strip()
        self.git('checkout','-q','main')
        self.commit('Base only')
        self.base=self.git('rev-parse','HEAD').strip()
        self.git('update-ref','refs/remotes/origin/main',self.base)
        self.git('checkout','-q','feature')
        self.head=self.git('rev-parse','HEAD').strip();self.reset_client()
        row=self.call()
        self.assertEqual(row['fill']['commits'],2)
        self.assertIn('- **Subject 😀**\n',row['body'])
        self.assertIn('- **Side subject**\n',row['body'])
        self.assertNotIn('merge',row['body']);self.assertNotIn('Base only',row['body'])

    def test_explicit_fields_override_independently_including_empty_body(self):
        for flags,title,body in [(['-t','Explicit'],'Explicit','Body\n\nLiteral $HOME\n'),
                                 (['-b',''],'Subject 😀',''),
                                 (['-t','Explicit','-b','Text'],'Explicit','Text')]:
            self.reset_client(); row=self.call(*flags)
            self.assertEqual((row['title'],row['body']),(title,body))

    def test_file_stdin_editor_draft_and_metadata_combinations(self):
        Path('body').write_bytes(b'File\r\n')
        row=self.call('-F','body','--draft','-a','@me','-l','one')
        self.assertEqual((row['title'],row['body']),('Draft: Subject 😀','File\r\n'))
        self.assertEqual(row['assignees'][0]['id'],1)
        self.reset_client()
        with patch('sys.stdin',io.StringIO('')),patch('gitverse_cli.comment_editor.edit_text',return_value='Edited\nBody') as editor:
            row=self.call('-F','-','--editor')
        self.assertEqual(editor.call_args.args[0], 'Subject 😀\n')
        self.assertEqual((row['title'],row['body']),('Edited','Body'))

    def test_short_alias_and_current_branch_and_configured_base(self):
        args=parser().parse_args(['pr','create','-R',REPO,'-f'])
        self.git('config','branch.feature.gh-merge-base','main')
        with patch('gitverse_cli.cli.Client',return_value=self.c): row=execute(args)
        self.assertEqual(row['head']['ref'],'feature')

    def test_default_base_resolved_without_affecting_fill(self):
        args=parser().parse_args(['pr','create','-R',REPO,'-f','--head','feature'])
        with patch('gitverse_cli.cli.Client',return_value=self.c): row=execute(args)
        self.assertEqual(row['base']['ref'],'main')

    def test_dry_run_offline_complete_local_defaults_no_auth_http_editor_or_push(self):
        self.c=Client(dry_run=True)
        with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('HTTP')),patch('socket.socket',side_effect=AssertionError('socket')),patch('gitverse_cli.comment_editor.edit_text',side_effect=AssertionError('editor')),patch('builtins.input',side_effect=AssertionError('prompt')):
            row=self.call('--dry-run')
            self.assertEqual(row['body']['title'],'Subject 😀')
            self.assertEqual(row['body']['body'],'Body\n\nLiteral $HOME\n')
            self.assertFalse(row['fill']['remote_verified'])
            self.assertEqual(row['network_requests'],0)
            self.assertTrue(row['validation']['final_request_known'])
            row=self.call('--dry-run','--editor','-a','@me')
            self.assertFalse(row['validation']['final_request_known'])
            self.assertEqual(row['editor']['seed']['title'],'Subject 😀')

    def test_missing_wrong_or_ambiguous_remote_refuses_before_network(self):
        self.git('remote','set-url','origin','https://gitverse.ru/wrong/repo.git')
        with self.assertRaises(GVError):self.call()
        self.git('remote','set-url','origin','https://gitverse.ru/'+REPO+'.git')
        self.git('remote','add','duplicate','https://gitverse.ru/'+REPO+'.git')
        with self.assertRaises(GVError):self.call()
        self.assertEqual(self.c.calls,[])

    def test_missing_shallow_grafted_and_empty_history_refuses_without_partial(self):
        self.git('update-ref','-d','refs/remotes/origin/main')
        with self.assertRaises(GVError):self.call()
        self.git('update-ref','refs/remotes/origin/main',self.base)
        Path('.git/shallow').write_text(self.base+'\n')
        with self.assertRaises(GVError) as e:self.call()
        self.assertEqual(e.exception.code,'incomplete_history');Path('.git/shallow').unlink()
        Path('.git/info/grafts').write_text('')
        with self.assertRaises(GVError):self.call()
        Path('.git/info/grafts').unlink()
        self.git('update-ref','refs/heads/feature',self.base)
        with self.assertRaises(GVError) as e:self.call()
        self.assertEqual(e.exception.code,'empty_commit_range')
        self.assertEqual(self.c.calls,[])

    def test_commit_count_output_time_and_final_text_bounds(self):
        self.commit('Second');self.reset_client()
        with patch.object(fill,'MAX_COMMITS',1):
            with self.assertRaises(GVError) as e:self.call()
            self.assertEqual(e.exception.code,'input_limit')
        with patch.object(fill,'MAX_BYTES',4):
            with self.assertRaises(GVError):self.call()
        with self.assertRaises(GVError) as e:fill.git_read(['status'],time.monotonic()-1)
        self.assertEqual(e.exception.code,'deadline_exceeded')
        for title,body in [('x'*1048577,''),('Fine','x'*1048577),('bad\nline',''),('Fine','\0')]:
            with patch.object(fill,'prepare',return_value=(title,body,{})):
                with self.assertRaises(GVError):self.call()
        self.assertEqual(self.c.calls,[])

    def test_malformed_commit_framing_and_invalid_utf8(self):
        original=fill.git_read
        for data in ('bad', 'bad\0subject\0body\0', self.head+'\0T\0B\0extra\0'):
            def read(args,deadline):return data if 'log' in args else original(args,deadline)
            with patch.object(fill,'git_read',side_effect=read):
                with self.assertRaises(GVError):self.call()
        tree=self.git('rev-parse','HEAD^{tree}').strip()
        raw=('tree '+tree+'\nparent '+self.base+'\nauthor X <x@y> 1 +0000\ncommitter X <x@y> 1 +0000\n\nBad ').encode()+b'\xff\n'
        result=subprocess.run(['git','hash-object','-t','commit','-w','--stdin'],input=raw,capture_output=True,check=True)
        self.git('update-ref','refs/heads/feature',result.stdout.decode().strip())
        with self.assertRaises(GVError) as e:self.call()
        self.assertEqual(e.exception.code,'usage_error')
        self.assertEqual(self.c.calls,[])

    def test_local_refs_changed_after_prepare_and_during_preflight_refuse(self):
        original=fill.recheck
        calls=0
        def recheck(*args):
            nonlocal calls
            calls+=1
            if calls==2:self.git('update-ref','refs/heads/feature',self.base)
            return original(*args)
        with patch.object(fill,'recheck',side_effect=recheck):
            with self.assertRaises(GVError) as e:self.call()
        self.assertEqual(e.exception.code,'concurrent_change')
        self.assertFalse(e.exception.details['mutation_attempted'])
        self.assertEqual(self.c.writes(),[])

    def test_wrong_remote_sha_name_or_malformed_dto_before_write(self):
        for value in ({},[],{'name':'foreign','commit':{'sha':self.base}},{'name':'main','commit':{'sha':'c'*40}},{'name':'main','commit':{'sha':True}}):
            self.reset_client(); self.intercept(lambda m,p,r,kw:value if '/branches/' in p else r)
            with self.assertRaises(GVError) as e:self.call()
            self.assertFalse(e.exception.details['mutation_attempted'])
            self.assertEqual(self.c.writes(),[])

    def test_wrong_scope_identity_and_permission_preflight(self):
        for target,value in [('repos/'+REPO,{}),('repos/'+REPO,{'id':7,'full_name':'wrong/repo','private':True}),('user',{'id':True,'login':'me'})]:
            self.reset_client();self.intercept(lambda m,p,r,kw:value if p==target else r)
            with self.assertRaises(GVError) as e:self.call()
            self.assertFalse(e.exception.details['mutation_attempted'])
            self.assertEqual(self.c.writes(),[])
        for status in (401,403,404):
            self.reset_client()
            def hook(m,p,r,kw):
                if '/branches/' in p:raise GVError('denied','http_error',status)
                return r
            self.intercept(hook)
            with self.assertRaises(GVError):self.call()
            self.assertEqual(self.c.writes(),[])

    def test_post_and_readback_concurrent_malformed_unknown_no_retry_recovery(self):
        for mode in ('sha','malformed','unknown','readback'):
            self.reset_client()
            def hook(m,p,r,kw):
                if m=='POST':
                    if mode=='sha':r['head']['sha']='c'*40
                    if mode=='malformed':return {'id':88,'number':8}
                    if mode=='unknown':raise GVError('uncertain','network_error')
                if mode=='readback' and p.endswith('/pulls/8'):raise GVError('timeout','deadline_exceeded')
                return r
            self.intercept(hook)
            with self.assertRaises(GVError) as e:self.call()
            self.assertTrue(e.exception.details['mutation_attempted'])
            self.assertEqual(len(self.c.writes()),1)
            self.assertEqual(e.exception.details['pull_number'],None if mode=='unknown' else 8)

    def test_equal_inputs_are_not_noop_or_retry(self):
        self.call();self.call()
        self.assertEqual(len(self.c.writes()),2)

    def test_public_redirect_refused_no_cookie_or_foreign_credential_forwarding(self):
        from unittest.mock import Mock
        from urllib.error import HTTPError
        c=Client(dry_run=True);c.dry_run=False;c.opener=Mock()
        c.opener.open.side_effect=[core.framed_response({'id':7,'full_name':REPO,'private':True}),core.framed_response({'id':1,'login':'me'}),HTTPError(c.base+'/repos/'+REPO+'/branches/main',307,'redirect',{'Location':'https://foreign.invalid/'},io.BytesIO(b'{}'))]
        self.c=c
        with self.assertRaises(GVError) as e:self.call()
        self.assertFalse(e.exception.details['mutation_attempted'])
        self.assertEqual(c.opener.open.call_count,3)
        for call in c.opener.open.call_args_list:
            self.assertTrue(call.args[0].full_url.startswith(c.base+'/'))
            self.assertIsNone(call.args[0].get_header('Cookie'))
