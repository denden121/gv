"""Editor input shares the verified discussion write pipeline."""
import copy
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.comment_editor import edit_text
from gitverse_cli.pr_comment import discussion_comment
from test_pr_comment import Fake


class EditorTests(unittest.TestCase):
    def setUp(self): self.client = Fake()

    def call(self, *flags):
        args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', '--editor', *flags])
        args.number = 9
        return discussion_comment(self.client, 'o/r', args)

    def writes(self): return [c for c in self.client.calls if c[0] != 'GET']

    def test_create_empty_seed_exact_text_independent_readback(self):
        with patch('gitverse_cli.comment_editor.edit_text', return_value=' Привет\r\n') as edit:
            row = self.call()
        self.assertEqual(edit.call_args.args[0], '')
        self.assertEqual(row['body'], ' Привет\r\n')
        self.assertEqual([c[0] for c in self.writes()], ['POST'])
        self.assertTrue(self.client.calls[-1][1].endswith('/issues/comments/99'))

    def test_edit_seed_and_changed_or_noop(self):
        for value, changed in [('edited', True), ('old', False)]:
            self.client = Fake()
            with patch('gitverse_cli.comment_editor.edit_text', return_value=value) as edit:
                result = self.call('--edit-last')
            self.assertEqual(edit.call_args.args[0], 'old')
            self.assertEqual(result['changed'], changed)
            self.assertEqual(len(self.writes()), int(changed))

    def test_absence_does_not_launch_editor_without_fallback(self):
        self.client.rows=[]
        with patch('gitverse_cli.comment_editor.edit_text', side_effect=AssertionError('editor')):
            with self.assertRaises(GVError) as caught:self.call('--edit-last')
        self.assertEqual(caught.exception.code,'comment_not_found')
        with patch('gitverse_cli.comment_editor.edit_text', return_value='fallback') as edit:
            self.assertEqual(self.call('--edit-last','--create-if-none')['body'],'fallback')
        self.assertEqual(edit.call_args.args[0],'')

    def test_invalid_combinations_before_client_or_selector(self):
        for flags in (['-b','x'],['-F','missing'],['--delete-last','--yes'],
                      ['--reply-to','1'],['--path','x','--line','1'],['--commit','a'*40]):
            args=parser().parse_args(['pr','comment','branch','-R','o/r','-e',*flags])
            with patch('gitverse_cli.cli.Client',side_effect=AssertionError('client')):
                with self.assertRaises(GVError) as caught:execute(args)
            self.assertEqual(caught.exception.code,'usage_error')

    def test_offline_dry_run_no_editor_network_or_draft(self):
        self.client.dry_run=True
        with patch('subprocess.Popen',side_effect=AssertionError('process')), patch('tempfile.TemporaryDirectory',side_effect=AssertionError('draft')):
            for flags in ((),('--edit-last',),('--edit-last','--create-if-none')):
                result=self.call(*flags)
                self.assertFalse(result['body_known'])
                self.assertNotIn('body',result)
                self.assertNotIn('comment_id',result)
        self.assertEqual(self.client.calls,[])

    def test_concurrent_comment_or_parent_during_editor_refuses(self):
        for mode in ('body','newest','parent'):
            self.client=Fake()
            def edit(*args):
                if mode=='body':self.client.rows[0]['updated_at']='concurrent'
                elif mode=='newest':self.client.rows.append(self.client.row(100,3))
                else:self.client.parent['head']['sha']='c'*40
                return 'changed'
            with patch('gitverse_cli.comment_editor.edit_text',side_effect=edit):
                with self.assertRaises(GVError) as caught:self.call('--edit-last')
            self.assertEqual(caught.exception.code,'concurrent_change')
            self.assertEqual(self.writes(),[])

    def test_denied_malformed_wrong_scope_preflight_never_opens_editor(self):
        for failure in ('denied','parent','user','catalog','scope','limit'):
            self.client=Fake()
            if failure=='denied':self.client.hook=lambda *a:(_ for _ in ()).throw(GVError('denied','http_error',403))
            elif failure=='parent':self.client.parent['number']=10
            elif failure=='catalog':self.client.rows=[{}]
            elif failure=='scope':self.client.rows[0]['issue_url']='https://evil.example/x'
            elif failure=='user':
                request=self.client.request
                self.client.request=lambda m,p,**kw: {'id':True} if p=='user' else request(m,p,**kw)
            with patch('gitverse_cli.comment_editor.edit_text',side_effect=AssertionError('editor')):
                with self.assertRaises(GVError):self.call('--edit-last',*(['--max-entries','1'] if failure=='limit' else []))
            self.assertEqual(self.writes(),[])

    def test_unknown_write_and_failed_readback_preserve_recovery(self):
        for edit in (False, True):
            for stage in ('write','readback'):
                self.client=Fake()
                if stage=='write':self.client.fail=GVError('unknown','network_error')
                else:
                    def hook(c,m,p):
                        if m=='GET' and '/issues/comments/' in p and self.writes():
                            raise GVError('readback failed','network_error')
                    self.client.hook=hook
                with patch('gitverse_cli.comment_editor.edit_text',return_value='changed'):
                    with self.assertRaises(GVError) as caught:self.call(*(['--edit-last'] if edit else []))
                self.assertTrue(caught.exception.details['mutation_attempted'])
                self.assertEqual(len(self.writes()),1)
                if edit or stage=='readback':self.assertIn('comment_id',caught.exception.details)

    def run_editor(self, code, initial='', seconds=3):
        with tempfile.TemporaryDirectory() as directory:
            script=Path(directory)/'editor script.py';script.write_text(code)
            command=shlex.join([sys.executable,str(script)])
            with patch.dict(os.environ,{'GV_EDITOR':command}):
                return edit_text(initial,time.monotonic()+seconds)

    def test_real_editor_private_file_literal_crlf_and_no_credentials(self):
        code="""import os, pathlib, stat, sys
p=pathlib.Path(sys.argv[1])
assert stat.S_IMODE(p.stat().st_mode)==0o600
assert p.read_bytes()==b'old'
assert not any(k in os.environ for k in ('GITVERSE_TOKEN','GV_TOKEN','GITVERSE_COOKIE_FILE','GITVERSE_CSRF_TOKEN'))
p.write_bytes(' Привет\\r\\n$HOME'.encode())
print('must not enter JSON')
"""
        with patch.dict(os.environ,{'GV_TOKEN':'synthetic','GITVERSE_COOKIE_FILE':'synthetic'}):
            self.assertEqual(self.run_editor(code,'old'),' Привет\r\n$HOME')

    def test_editor_priority_argv_no_shell_and_cleanup(self):
        observed={}
        class Process:
            def __init__(self, argv, **kwargs):
                observed.update(argv=argv,kwargs=kwargs)
                Path(argv[-1]).write_text('new')
            def wait(self,timeout):return 0
        with patch.dict(os.environ,{'GV_EDITOR':"'my editor' --wait '$HOME'",'VISUAL':'bad','EDITOR':'bad'}),patch('subprocess.Popen',Process):
            self.assertEqual(edit_text('',time.monotonic()+3),'new')
        self.assertEqual(observed['argv'][:-1],['my editor','--wait','$HOME'])
        self.assertNotIn('shell',observed['kwargs'])
        self.assertFalse(Path(observed['argv'][-1]).exists())

    def test_editor_failure_timeout_invalid_or_oversize_input(self):
        for code in ("raise SystemExit(4)","import pathlib,sys;pathlib.Path(sys.argv[1]).write_bytes(b'\\xff')",
                     "import pathlib,sys;pathlib.Path(sys.argv[1]).write_bytes(b'\\0')",
                     "import pathlib,sys;pathlib.Path(sys.argv[1]).write_bytes(b'x'*1048577)",
                     "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('  ')",
                     "import pathlib,sys; p=pathlib.Path(sys.argv[1]);p.unlink();p.symlink_to('/dev/null')"):
            with self.assertRaises(GVError):self.run_editor(code)
        with self.assertRaises(GVError) as caught:self.run_editor('import time;time.sleep(5)',seconds=.08)
        self.assertEqual(caught.exception.code,'deadline_exceeded')

    def test_start_error_bad_command_and_expired_deadline_sanitized(self):
        for command in ('nonexistent-gv-editor-5c9e50c31bad',"'",'   '):
            with patch.dict(os.environ,{'GV_EDITOR':command}):
                with self.assertRaises(GVError):edit_text('',time.monotonic()+1)
        with self.assertRaises(GVError):edit_text('',time.monotonic()-1)

    def test_editor_error_sends_no_mutation(self):
        with patch('gitverse_cli.comment_editor.edit_text',side_effect=GVError('failure','editor_error')):
            with self.assertRaises(GVError):self.call('--edit-last')
        self.assertEqual(self.writes(),[])

    def test_public_only(self):
        self.client.backend='web'
        with patch('gitverse_cli.comment_editor.edit_text',side_effect=AssertionError('editor')):
            with self.assertRaises(GVError):self.call()
        self.assertEqual(self.client.calls,[])

    def test_editor_transport_redirect_no_credential_forward_or_retry(self):
        import io
        import json
        import urllib.error
        from unittest.mock import Mock
        from gitverse_cli.client import Client, NoRedirect
        class Response(io.BytesIO): status = 200
        client = Client(dry_run=True); client.dry_run = False
        client.headers['Authorization'] = 'Bearer synthetic-test-token'
        client.opener = Mock()
        client.opener.open.side_effect = [Response(json.dumps(r).encode()) for r in
            (self.client.parent, {'id': 3, 'login': 'me'}, self.client.parent)] + [
            urllib.error.HTTPError(client.base, 307, 'refused', {'Location': 'https://elsewhere.invalid/x'}, None)]
        args = parser().parse_args(['pr','comment','9','-R','o/r','-e']);args.number=9
        with patch('gitverse_cli.comment_editor.edit_text',return_value='edited'):
            with self.assertRaises(GVError) as caught:discussion_comment(client,'o/r',args)
        self.assertEqual(caught.exception.status,307)
        self.assertTrue(caught.exception.details['mutation_attempted'])
        self.assertEqual(client.opener.open.call_count,4)
        for call in client.opener.open.call_args_list:
            request=call.args[0]
            self.assertTrue(request.full_url.startswith(client.base+'/'))
            self.assertIsNone(request.get_header('Cookie'))
        self.assertIsNone(NoRedirect().redirect_request(None,None,307,'',{},'https://elsewhere.invalid/x'))

    def test_editor_response_deadline_and_completeness_bounds(self):
        for after in (False,True):
            for code in ('deadline_exceeded','response_limit','incomplete_response'):
                self.client=Fake()
                def hook(c,m,p):
                    if (after and '/issues/comments/' in p) or (not after and '/pulls/' in p):
                        raise GVError('bounded',code)
                self.client.hook=hook
                with patch('gitverse_cli.comment_editor.edit_text',return_value='edited'):
                    with self.assertRaises(GVError) as caught:self.call()
                self.assertEqual(caught.exception.code,code)
                self.assertEqual(len(self.writes()),int(after))
                if after:self.assertEqual(caught.exception.details['comment_id'],99)
                self.assertTrue(all(c[4]['require_complete'] and c[4]['max_response_bytes']==33554432 for c in self.client.calls))
                self.assertEqual(len({c[4]['deadline'] for c in self.client.calls}),1)

    def test_short_alias_and_discovery(self):
        self.assertTrue(parser().parse_args(['pr','comment','9','-e']).editor)
        info=execute(parser().parse_args(['agent-info']))
        self.assertIn('-e',info['discussion_comment_editor']['flags'])

if __name__=='__main__':unittest.main()
