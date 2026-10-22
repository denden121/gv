"""Behavioral saved-input recovery contracts; no remote fixture writes."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from gitverse_cli.pr_create_recover import Recovery
from test_metadata import Public, REPO


class RecoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        self.c = Public()
        original = tempfile.mkstemp
        save = patch('gitverse_cli.pr_create_recover.tempfile.mkstemp',
                     side_effect=lambda **kw: original(dir=self.dir, **kw))
        save.start(); self.addCleanup(save.stop)

    def args(self, *flags):
        return parser().parse_args(['pr', 'create', '-R', REPO, *flags])

    def run_create(self, *flags, client=None):
        with patch('gitverse_cli.cli.Client', return_value=client or self.c):
            return execute(self.args(*flags))

    def fail_preflight(self, *flags):
        with patch.object(self.c, 'request', side_effect=GVError('Denied', 'http_error', 403)):
            with self.assertRaises(GVError) as caught:
                self.run_create('--head', 'feature', '--base', 'main', '--title', 'Saved Б',
                                '--body', ' Literal\r\n`$HOME`\n', *flags)
        error = caught.exception
        self.assertFalse(error.details['mutation_attempted'])
        path = Path(error.details['recover_file'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        return path

    def write(self, row, filename='input.json'):
        path = self.dir / filename
        path.write_text(json.dumps(row, ensure_ascii=False))
        path.chmod(0o600)
        return path

    def dry(self, path, *flags):
        return self.run_create('--recover', str(path), '--dry-run', *flags, client=Client(dry_run=True))

    def test_failed_input_restored_exactly_without_prompt_or_editor(self):
        path = self.fail_preflight()
        with patch('builtins.input', side_effect=AssertionError('prompt')), \
             patch('gitverse_cli.comment_editor.edit_text', side_effect=AssertionError('editor')):
            row = self.run_create('--recover', str(path))
        self.assertEqual((row['title'], row['body']), ('Saved Б', ' Literal\r\n`$HOME`\n'))
        self.assertTrue(row['create_verification']['readback'])
        self.assertEqual(len(self.c.writes()), 1)
        saved = json.loads(path.read_text())
        self.assertEqual((saved['pull_id'], saved['pull_number'], saved['mutation_outcome']), (88, 8, 'confirmed'))

    def test_second_recover_refuses_successful_input_before_any_network(self):
        path = self.fail_preflight()
        self.run_create('--recover', str(path))
        with patch.object(self.c, 'request', side_effect=AssertionError('network')):
            with self.assertRaises(GVError) as caught:
                self.run_create('--recover', str(path))
        self.assertEqual(caught.exception.code, 'recovery_requires_reconciliation')
        self.assertEqual(caught.exception.details['pull_id'], 88)

    def test_explicit_title_body_file_and_stdin_override_independently(self):
        path = self.fail_preflight()
        body = self.dir/'body.md'; body.write_bytes(b'File\r\n')
        cases = [(['--title', 'Override'], 'Override', ' Literal\r\n`$HOME`\n'),
                 (['--body', ''], 'Saved Б', ''),
                 (['--body-file', str(body)], 'Saved Б', 'File\r\n'),
                 (['--body-file', '-'], 'Saved Б', 'stdin\r\n')]
        for flags, title, text in cases:
            with self.subTest(flags=flags), patch('sys.stdin', io.StringIO('stdin\r\n')):
                row = self.dry(path, *flags)
            self.assertEqual(row['body']['title'], title)
            self.assertEqual(row['body']['body'], text)

    def test_dry_run_offline_does_not_change_or_create_files(self):
        path = self.fail_preflight(); before = path.read_bytes(); files = list(self.dir.iterdir())
        with patch('socket.socket', side_effect=AssertionError('socket')), \
             patch('gitverse_cli.client.load_auth', side_effect=AssertionError('auth')), \
             patch('subprocess.Popen', side_effect=AssertionError('process')), \
             patch('builtins.input', side_effect=AssertionError('prompt')):
            row = self.dry(path)
        self.assertTrue(row['recovered_input']['replay_allowed'])
        self.assertEqual(row['network_requests'], 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.dir.iterdir()), files)

    def test_recovered_draft_assignee_label_and_permission_combinations(self):
        path = self.fail_preflight('--draft', '--assignee', '@me', '--label', 'one', '--no-maintainer-edit')
        original = self.c.request
        def request(method, target, *args, **kwargs):
            row = original(method, target, *args, **kwargs)
            if target.endswith('/pulls') and method == 'POST' or target.endswith('/pulls/8'):
                row['maintainer_can_modify'] = False
            return row
        with patch.object(self.c, 'request', side_effect=request):
            row = self.run_create('--recover', str(path))
        self.assertEqual(row['title'], 'Draft: Saved Б')
        self.assertTrue(row['draft'])
        self.assertFalse(row['maintainer_can_modify'])
        self.assertEqual(row['assignees'][0]['id'], 1)
        self.assertEqual(row['labels'][0]['id'], 11)

    def test_metadata_override_and_unresolved_preview(self):
        path = self.fail_preflight('--assignee', '@me', '--label', 'one')
        row = self.dry(path, '--label', 'two')
        self.assertFalse(row['validation']['metadata_resolved'])
        self.assertFalse(row['validation']['final_request_known'])
        with patch('gitverse_cli.cli.Client', return_value=self.c):
            row = execute(self.args('--recover', str(path), '--label', 'two'))
        self.assertEqual(row['labels'][0]['name'], 'two')

    def test_editor_uses_saved_text_as_seed(self):
        path = self.fail_preflight()
        with patch('gitverse_cli.comment_editor.edit_text', return_value='Edited\nNew body') as edit:
            row = self.run_create('--recover', str(path), '--editor')
        self.assertEqual(edit.call_args.args[0], 'Saved Б\n Literal\r\n`$HOME`\n')
        self.assertEqual((row['title'], row['body']), ('Edited', 'New body'))

    def test_fill_keeps_recovered_explicit_text(self):
        path = self.fail_preflight()
        with patch('gitverse_cli.pr_create_fill.prepare', return_value=('Generated', 'Generated body', {'mode':'first'})):
            row = self.dry(path, '--fill-first')
        self.assertEqual(row['body']['title'], 'Saved Б')
        self.assertEqual(row['body']['body'], ' Literal\r\n`$HOME`\n')

    def test_recovery_scope_repo_origin_and_branches_refuse_without_network(self):
        path = self.fail_preflight(); original = json.loads(path.read_text())
        for key, value in [('repository','other/repo'), ('public_api','https://other.invalid'),
                           ('public_api','https://api.gitverse.ru/path')]:
            with self.subTest(key=key,value=value):
                row = copy.deepcopy(original);row[key] = value
                with self.assertRaises(GVError) as caught:
                    self.dry(self.write(row))
                self.assertEqual(caught.exception.code,'recovery_scope_mismatch')
        for flag in ('--head','--base'):
            with self.assertRaises(GVError) as caught:
                self.dry(path,flag,'other')
            self.assertEqual(caught.exception.code,'recovery_scope_mismatch')
        self.assertFalse(self.c.writes())

    def test_invalid_json_dto_version_metadata_and_mutation_state(self):
        path = self.fail_preflight(); original = json.loads(path.read_text())
        cases = [None, [], {}, {**original,'version':True}, {**original,'command':'issue create'},
                 {**original,'unexpected':'Authorization'}, {**original,'mutation_attempted':1},
                 {**original,'mutation_outcome':'unknown'}, {**original,'pull_id':88}]
        for key,value in [('title',None),('title','bad\nline'),('body','bad\0body'),('head',123),
                          ('base','../main'),('draft',1),('no_maintainer_edit',None),
                          ('label',{}),('assignee',[1]),('reviewer',[]),('project',['\0']),('milestone',12)]:
            row = copy.deepcopy(original);row['input'][key] = value;cases.append(row)
        for row in cases:
            with self.subTest(row_type=type(row).__name__), self.assertRaises(GVError):
                self.dry(self.write(row))
        for raw in ('{', '{"version":1,"version":1}', 'NaN', '"\\ud800"'):
            path.write_text(raw)
            with self.assertRaises(GVError):self.dry(path)
        path.write_bytes(b'\xff')
        with self.assertRaises(GVError):self.dry(path)

    def test_nonregular_symlink_hardlink_missing_stdin_and_nonprivate_files_refuse(self):
        path = self.fail_preflight()
        link = self.dir/'symlink';link.symlink_to(path)
        hard = self.dir/'hard';os.link(path,hard)
        fifo = self.dir/'fifo';os.mkfifo(fifo,0o600)
        for name in (link,hard,fifo,self.dir,self.dir/'missing','-',''):
            with self.subTest(name=str(name)), self.assertRaises(GVError):self.dry(name)
        hard.unlink();path.chmod(0o644)
        with self.assertRaises(GVError):self.dry(path)

    def test_wrong_file_owner_refuses(self):
        from types import SimpleNamespace
        path = self.fail_preflight()
        original = os.fstat
        def fstat(fd):
            info=original(fd)
            return SimpleNamespace(**{key:getattr(info,key) for key in ('st_mode','st_nlink')},st_uid=info.st_uid+1)
        with patch('gitverse_cli.pr_create_recover.os.fstat',side_effect=fstat),self.assertRaises(GVError):
            self.dry(path)

    def test_size_and_deadline_bounds(self):
        path = self.fail_preflight()
        with patch('gitverse_cli.pr_create_recover.MAX_BYTES',16),self.assertRaises(GVError) as caught:
            self.dry(path)
        self.assertEqual(caught.exception.code,'input_limit')
        args=self.args('--recover',str(path))
        with self.assertRaises(GVError) as caught:
            with Recovery(args,REPO,self.c,time.monotonic()-1):pass
        self.assertEqual(caught.exception.code,'deadline_exceeded')

    def test_default_base_failure_preserves_text_and_unresolved_base(self):
        with patch('gitverse_cli.pr_create.configured_base',return_value=None), \
             patch.object(self.c,'request',side_effect=GVError('Denied','http_error',401)), \
             self.assertRaises(GVError) as caught:
            self.run_create('--head','feature','--title','Default base','--body','Saved text')
        path=Path(caught.exception.details['recover_file'])
        self.assertIsNone(json.loads(path.read_text())['input']['base'])
        row=self.dry(path,'--base','main')
        self.assertEqual(row['body']['body'],'Saved text')
        self.assertEqual(row['body']['base'],'main')

    def test_unresolved_head_and_unfinished_editor_seed_can_be_completed(self):
        with patch('gitverse_cli.pr_create.select_head',side_effect=GVError('No Git','git_error')), \
             self.assertRaises(GVError) as caught:
            self.run_create('--editor','--base','main','--body','Seed body')
        path=Path(caught.exception.details['recover_file'])
        row=self.dry(path,'--head','feature','--title','Completed')
        self.assertEqual(row['body']['title'],'Completed')
        with patch('gitverse_cli.comment_editor.edit_text',return_value='Completed\nSeed body'):
            row=self.run_create('--recover',str(path),'--head','feature','--editor')
        self.assertEqual(row['body'],'Seed body')

    def test_concurrent_recovery_lock_refuses_second_invocation(self):
        path=self.fail_preflight();args=self.args('--recover',str(path))
        with Recovery(args,REPO,self.c,time.monotonic()+60):
            with self.assertRaises(GVError) as caught:self.run_create('--recover',str(path))
        self.assertEqual(caught.exception.code,'recovery_busy')
        self.assertFalse(self.c.writes())

    def test_changed_file_refuses_before_write(self):
        path=self.fail_preflight();original=self.c.request
        def request(method,target,*args,**kwargs):
            row=original(method,target,*args,**kwargs)
            if target=='user':path.write_bytes(path.read_bytes()+b' ')
            return row
        with patch.object(self.c,'request',side_effect=request),self.assertRaises(GVError) as caught:
            self.run_create('--recover',str(path))
        self.assertEqual(caught.exception.code,'concurrent_change')
        self.assertFalse(caught.exception.details['mutation_attempted'])
        self.assertFalse(self.c.writes())

    def test_failed_guard_persistence_prevents_post(self):
        path=self.fail_preflight()
        with patch('gitverse_cli.pr_create_recover.os.fsync',side_effect=OSError()),self.assertRaises(GVError) as caught:
            self.run_create('--recover',str(path))
        self.assertEqual(caught.exception.code,'recovery_save_failed')
        self.assertFalse(caught.exception.details['mutation_attempted'])
        self.assertTrue(caught.exception.details['recover_save_failed'])
        self.assertFalse(self.c.writes())

    def test_unknown_write_guard_is_persisted_before_request_and_never_replayed(self):
        path=self.fail_preflight();original=self.c.request
        def request(method,target,*args,**kwargs):
            if method=='POST':
                state=json.loads(path.read_text())
                self.assertTrue(state['mutation_attempted'])
                self.assertEqual(state['mutation_outcome'],'unknown')
                raise GVError('Lost response','network_error')
            return original(method,target,*args,**kwargs)
        with patch.object(self.c,'request',side_effect=request),self.assertRaises(GVError) as caught:
            self.run_create('--recover',str(path))
        error=caught.exception
        self.assertEqual(error.details['mutation_outcome'],'unknown')
        self.assertFalse(error.details['recover_replay_allowed'])
        for source in (path,Path(error.details['recover_file'])):
            with patch.object(self.c,'request',side_effect=AssertionError('network')),self.assertRaises(GVError) as blocked:
                self.run_create('--recover',str(source))
            self.assertEqual(blocked.exception.code,'recovery_requires_reconciliation')
            before=source.read_bytes();row=self.dry(source)
            self.assertFalse(row['recovered_input']['replay_allowed'])
            self.assertEqual(source.read_bytes(),before)

    def test_malformed_wrong_scope_author_and_concurrent_readback_keep_ids(self):
        for problem in ('dto','scope','author','concurrent'):
            with self.subTest(problem=problem):
                self.c=Public();path=self.fail_preflight();original=self.c.request
                def request(method,target,*args,**kwargs):
                    row=original(method,target,*args,**kwargs)
                    if method=='POST' or target.endswith('/pulls/8'):
                        if problem=='dto':row['draft']='false'
                        if problem=='scope':row['head']['repo']['id']=99
                        if problem=='author':row['user']['id']=99
                        if problem=='concurrent' and method=='GET':row['body']='Changed'
                    return row
                with patch.object(self.c,'request',side_effect=request),self.assertRaises(GVError) as caught:
                    self.run_create('--recover',str(path))
                details=caught.exception.details
                self.assertEqual((details['pull_id'],details['pull_number']),(88,8))
                saved=json.loads(Path(details['recover_file']).read_text())
                self.assertEqual((saved['pull_id'],saved['pull_number']),(88,8))
                self.assertEqual(len(self.c.writes()),1)
                self.assertFalse(details['recover_replay_allowed'])

    def test_preflight_permission_failure_keeps_reusable_input_and_original_error(self):
        path=self.fail_preflight()
        with patch.object(self.c,'request',side_effect=GVError('Forbidden','http_error',403)),self.assertRaises(GVError) as caught:
            self.run_create('--recover',str(path))
        self.assertEqual(caught.exception.status,403)
        self.assertTrue(caught.exception.details['recover_replay_allowed'])
        self.assertFalse(json.loads(path.read_text())['mutation_attempted'])

    def test_invalid_unicode_metadata_does_not_mask_original_usage_error(self):
        with self.assertRaises(GVError) as caught:
            self.run_create('--head','feature','--base','main','--title','Title','--label','\ud800')
        self.assertEqual(caught.exception.code,'usage_error')
        self.assertTrue(caught.exception.details['recover_save_failed'])
        self.assertFalse(self.c.writes())

    def test_saved_input_contains_no_transport_credentials_or_server_details(self):
        self.c.headers = {'Authorization':'Bearer synthetic-secret-only','Cookie':'synthetic-cookie-only'}
        with patch.object(self.c,'request',side_effect=GVError('private-server-body','http_error',403,
                          details={'raw':'private-server-body'})),self.assertRaises(GVError) as caught:
            self.run_create('--head','feature','--base','main','--title','Title')
        raw=Path(caught.exception.details['recover_file']).read_text()
        for value in ('synthetic-secret-only','synthetic-cookie-only','private-server-body','Authorization','Cookie'):
            self.assertNotIn(value,raw)

    def test_transport_bounds_and_redirect_refusal_keep_credentials_at_origin(self):
        import urllib.error
        from unittest.mock import Mock
        path=self.fail_preflight();client=Client(dry_run=True);client.dry_run=False
        client.headers['Authorization']='Bearer synthetic-test-only'
        client.opener=Mock()
        client.opener.open.side_effect=urllib.error.HTTPError('https://api.gitverse.ru/repos/owner/repo',302,'redirect',{'Location':'https://other.invalid'},io.BytesIO(b'{}'))
        with self.assertRaises(GVError) as caught:self.run_create('--recover',str(path),client=client)
        self.assertEqual(caught.exception.status,302)
        client.opener.open.assert_called_once()
        self.assertTrue(client.opener.open.call_args.args[0].full_url.startswith('https://api.gitverse.ru/'))
        self.assertFalse(caught.exception.details['mutation_attempted'])
        options=[];self.c=Public();original=self.c.request
        def request(method,target,*args,**kwargs):
            options.append(kwargs);return original(method,target,*args,**kwargs)
        with patch.object(self.c,'request',side_effect=request):self.run_create('--recover',str(path))
        self.assertTrue(all(k['require_complete'] and k['max_response_bytes']==33554432 and k['deadline']>0 for k in options))

    def test_metadata_pagination_bound_refuses_without_creation(self):
        path=self.fail_preflight('--label','one');original=self.c.request
        def request(method,target,*args,**kwargs):
            if target.endswith('/labels'):return copy.deepcopy(self.c.labels)
            return original(method,target,*args,**kwargs)
        with patch.object(self.c,'request',side_effect=request),self.assertRaises(GVError):
            self.run_create('--recover',str(path))
        self.assertFalse(self.c.writes())

    def test_source_namespace_not_modified_and_discovery(self):
        path=self.fail_preflight();args=self.args('--recover',str(path))
        with patch('gitverse_cli.cli.Client',return_value=self.c):execute(args)
        self.assertIsNone(args.title)
        info=execute(parser().parse_args(['agent-info']))
        self.assertEqual(info['pr_create_recover']['flags'],['--recover'])
