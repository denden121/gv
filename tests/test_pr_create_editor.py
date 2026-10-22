"""Editor flag contracts; synthetic HTTP, with real local editor subprocesses."""
import io
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
import test_pr_create as core
from test_metadata import Public, REPO


class CreateEditorTests(unittest.TestCase):
    setUp = core.CreateTests.setUp
    intercept = core.CreateTests.intercept

    def call(self, *flags):
        args = parser().parse_args(['pr', 'create', '-R', REPO, '--head', 'feature', '--base', 'main', *flags])
        with patch('gitverse_cli.cli.Client', return_value=self.c):
            return execute(args)

    def edited(self, value, *flags):
        with patch('gitverse_cli.comment_editor.edit_text', return_value=value):
            return self.call('--editor', *flags)

    def test_no_title_needed_and_literal_first_line_remaining_body(self):
        for value, title, body in [('Title\n Body\r\n\n', 'Title', ' Body\r\n\n'),
                                   ('Заголовок 😀\r\n$HOME\r\n', 'Заголовок 😀', '$HOME\r\n'),
                                   ('Only title', 'Only title', ''), ('Title\n', 'Title', '')]:
            self.c = Public()
            row = self.edited(value)
            self.assertEqual((row['title'], row['body']), (title, body))
            self.assertEqual(len(self.c.writes()), 1)
            self.assertTrue(row['create_verification']['readback'])
            self.assertTrue(self.c.calls[-1][1].endswith('/pulls/8'))

    def test_explicit_title_and_body_are_editable_seeds_even_when_both_present(self):
        for flags in [[], ['--title', 'Seed'], ['--body', 'Seed body'], ['-t', 'Seed', '-b', 'Seed body']]:
            self.c = Public()
            with patch('gitverse_cli.comment_editor.edit_text', return_value='Replaced\nEdited') as edit:
                row = self.call('-e', *flags)
            title = 'Seed' if '-t' in flags or '--title' in flags else ''
            body = 'Seed body' if '-b' in flags or '--body' in flags else ''
            self.assertEqual(edit.call_args.args[0], title + '\n' + body)
            self.assertEqual(row['title'], 'Replaced')
            self.assertEqual(row['body'], 'Edited')

    def test_file_and_stdin_seed_with_draft_and_public_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            f = Path(directory)/'seed'; f.write_bytes(b'body\r\n')
            for filename in (str(f), '-'):
                self.c = Public()
                with patch('sys.stdin', io.StringIO('body\r\n')), patch('gitverse_cli.comment_editor.edit_text', return_value='Final\ntext') as edit:
                    row = self.call('-e', '--body-file', filename, '--draft', '-a', '@me', '-l', 'one')
                self.assertEqual(edit.call_args.args[0], '\nbody\r\n')
                self.assertEqual(row['title'], 'Draft: Final')
                self.assertEqual(row['assignees'][0]['id'], 1)
                self.assertEqual(row['labels'][0]['id'], 11)

    def test_without_editor_title_is_still_required(self):
        with self.assertRaises(GVError) as caught:
            self.call()
        self.assertEqual(caught.exception.code, 'usage_error')
        self.assertEqual(self.c.calls, [])

    def test_blank_title_and_individual_bounds(self):
        for value in ('\nBody', ' \nBody', 'bad\rtitle\nbody', 'x'*1048577+'\nbody', 'T\n'+'x'*1048577):
            self.c = Public()
            with self.assertRaises(GVError): self.edited(value)
            self.assertEqual(self.c.calls, [])
        self.assertEqual(len(self.edited('T\n'+'x'*1048576)['body']), 1048576)

    def test_seed_invalid_unicode_nul_multiline_size_before_editor_or_network(self):
        for flags in (['-t', 'bad\nseed'], ['-t', '\ud800'], ['-b', '\ud800'], ['-b', '\0'], ['-b', 'x'*1048577]):
            with patch('gitverse_cli.comment_editor.edit_text', side_effect=AssertionError('editor')):
                with self.assertRaises(GVError): self.call('-e', *flags)
            self.assertEqual(self.c.calls, [])

    def test_offline_dry_run_no_editor_auth_network_or_draft_and_no_final_text(self):
        self.c = Client(dry_run=True)
        with patch('subprocess.Popen', side_effect=AssertionError('process')), patch('tempfile.TemporaryDirectory', side_effect=AssertionError('draft')), patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')), patch('builtins.input', side_effect=AssertionError('prompt')):
            for flags in ([], ['-t', 'seed', '-b', 'body'], ['-a', '@me', '-l', 'one']):
                result = self.call('--dry-run', '-e', *flags)
                self.assertFalse(result['validation']['final_request_known'])
                self.assertEqual(result['network_requests'], 0)
                self.assertFalse(result['editor']['launched'])
                self.assertFalse(result['editor']['title_known'])
                request = result.get('base_request_template', result)
                self.assertNotIn('title', request['body'])
                self.assertNotIn('body', request['body'])
                self.assertNotIn('number', result)

    def test_editor_failure_before_any_http(self):
        for code in ('editor_error', 'deadline_exceeded', 'usage_error'):
            with patch('gitverse_cli.comment_editor.edit_text', side_effect=GVError('invalid', code)):
                with self.assertRaises(GVError): self.call('-e')
            self.assertEqual(self.c.calls, [])

    def test_identity_scope_malformed_and_permission_errors_before_write(self):
        for target, replacement in [('repos/owner/repo', {}), ('repos/owner/repo', {'id':7,'full_name':'wrong/repo','private':True}), ('user', {'id':True,'login':'me'})]:
            self.c = Public()
            self.intercept(lambda m,p,r,kw: replacement if p == target else r)
            with self.assertRaises(GVError) as caught: self.edited('T\nB')
            self.assertFalse(caught.exception.details['mutation_attempted'])
            self.assertEqual(self.c.writes(), [])
        for status in (401,403,404):
            self.c = Public()
            self.c.request = lambda *a,**kw: (_ for _ in ()).throw(GVError('denied','http_error',status))
            with self.assertRaises(GVError): self.edited('T\nB')
            self.assertEqual(self.c.writes(), [])

    def test_malformed_post_concurrent_and_unknown_outcome_retain_recovery_no_retry(self):
        for mode in ('malformed', 'changed', 'unknown', 'readback'):
            self.c = Public()
            def hook(m,p,r,kw):
                if mode == 'unknown' and m == 'POST': raise GVError('uncertain','network_error')
                if mode == 'malformed' and m == 'POST': return {'id':88,'number':8}
                if p.endswith('/pulls/8'):
                    if mode == 'changed': return {**r,'title':'Concurrent'}
                    if mode == 'readback': raise GVError('failed','incomplete_response')
                return r
            self.intercept(hook)
            with self.assertRaises(GVError) as caught: self.edited('T\nB')
            self.assertEqual(len(self.c.writes()), 1)
            self.assertTrue(caught.exception.details['mutation_attempted'])
            self.assertEqual(caught.exception.details['head'], 'feature')
            if mode != 'unknown': self.assertEqual(caught.exception.details['pull_id'], 88)

    def test_unchanged_editor_seed_still_creates_once_never_noop(self):
        self.edited('T\nB', '-t', 'T', '-b', 'B')
        self.assertEqual(len(self.c.writes()), 1)
        self.edited('T\nB', '-t', 'T', '-b', 'B')
        self.assertEqual(len(self.c.writes()), 2)

    def test_response_time_size_and_pagination_bounds(self):
        for after in (False,True):
            for code in ('deadline_exceeded','response_limit','incomplete_response'):
                self.c = Public()
                options=[]
                def hook(m,p,r,kw):
                    options.append(kw)
                    if (after and p.endswith('/pulls/8')) or (not after and p=='user'): raise GVError('bound',code)
                    return r
                self.intercept(hook)
                with self.assertRaises(GVError): self.edited('T\nB')
                self.assertEqual(len(self.c.writes()), int(after))
                self.assertTrue(all(k['require_complete'] and k['max_response_bytes']==33554432 for k in options))
                self.assertEqual(len({k['deadline'] for k in options}), 1)
        self.c = Public()
        self.intercept(lambda m,p,r,kw: [{'id': kw and len(self.c.calls), 'name':str(len(self.c.calls)), 'exclusive':False, 'is_archived':False}] if p.endswith('/labels') else r)
        with self.assertRaises(GVError) as caught: self.edited('T\nB','-l','one')
        self.assertEqual(caught.exception.code,'pagination_limit')
        self.assertEqual(self.c.writes(), [])

    def test_real_editor_combined_seed_literal_crlf_credentials_and_json_streams(self):
        with tempfile.TemporaryDirectory() as directory:
            script=Path(directory)/'editor script.py'
            script.write_text("""import os,pathlib,stat,sys
p=pathlib.Path(sys.argv[-1])
assert p.read_bytes()==b'Seed\\nBody'
assert stat.S_IMODE(p.stat().st_mode)==0o600
assert not any(k in os.environ for k in ('GITVERSE_TOKEN','GV_TOKEN','GITVERSE_COOKIE_FILE','GITVERSE_CSRF_TOKEN'))
p.write_bytes('Final 😀\\r\\n Body\\r\\n'.encode())
print('editor output is isolated')
""")
            with patch.dict(os.environ, {'GV_EDITOR':shlex.join([sys.executable,str(script)]),'GV_TOKEN':'synthetic'}):
                row=self.call('-e','-t','Seed','-b','Body')
            self.assertEqual(row['title'], 'Final 😀')
            self.assertEqual(row['body'], ' Body\r\n')

    def test_real_editor_invalid_deleted_nonregular_failed_and_timedout_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            script=Path(directory)/'editor.py'
            for code in ("raise SystemExit(3)", "p.write_bytes(b'\\xff')", "p.write_bytes(b'T\\n\\0')", "p.unlink()", "p.unlink();p.symlink_to('/dev/null')", "p.write_bytes(b'T\\n'+b'x'*2097154)", "import time;time.sleep(5)"):
                self.c=Public()
                script.write_text('import pathlib,sys\np=pathlib.Path(sys.argv[-1])\n'+code)
                with patch.dict(os.environ,{'GV_EDITOR':shlex.join([sys.executable,str(script)])}):
                    with self.assertRaises(GVError):self.call('-e','--timeout','0.15')
                self.assertEqual(self.c.calls,[])

    def test_public_only_and_discovery(self):
        self.c.backend='web'
        with patch('gitverse_cli.comment_editor.edit_text', side_effect=AssertionError('editor')):
            with self.assertRaises(GVError):self.call('-e','--backend','web')
        self.assertEqual(self.c.calls,[])
        self.assertIn('--editor',execute(parser().parse_args(['agent-info']))['pr_create_editor']['flags'])

    def test_redirect_does_not_forward_credentials_or_retry(self):
        import urllib.error
        from unittest.mock import Mock
        from gitverse_cli.client import NoRedirect
        c=Client(dry_run=True);c.dry_run=False;c.headers['Authorization']='Bearer synthetic'
        c.opener=Mock()
        c.opener.open.side_effect=[core.framed_response({'id':7,'full_name':REPO,'private':True}),core.framed_response({'id':1,'login':'me'}),urllib.error.HTTPError(c.base,307,'redirect',{'Location':'https://foreign.invalid/'},None)]
        self.c=c
        with self.assertRaises(GVError) as caught:self.edited('T\nB')
        self.assertTrue(caught.exception.details['mutation_attempted'])
        self.assertEqual(c.opener.open.call_count,3)
        for call in c.opener.open.call_args_list:
            self.assertTrue(call.args[0].full_url.startswith(c.base+'/'))
            self.assertIsNone(call.args[0].get_header('Cookie'))
        self.assertIsNone(NoRedirect().redirect_request(None,None,307,'',{},'https://foreign.invalid/'))
