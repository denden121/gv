"""Template input and single-create behavior, using synthetic HTTP only."""
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
import test_pr_create as core
from test_metadata import Public, REPO


class TemplateTests(unittest.TestCase):
    intercept = core.CreateTests.intercept

    def setUp(self):
        core.CreateTests.setUp(self)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name)/'template.md'
        self.text = '## Change Б 😀\r\n\n<!-- literal $HOME -->\n'
        self.file.write_bytes(self.text.encode())

    def call(self, *flags):
        args = parser().parse_args(['pr', 'create', '-R', REPO, '--head', 'feature', '--base', 'main', *flags])
        with patch('gitverse_cli.cli.Client', return_value=self.c):
            return execute(args)

    def create(self, *flags):
        return self.call('--title', 'Template', '--template', str(self.file), *flags)

    def test_literal_body_alias_and_empty_file(self):
        for text in (self.text, '', '---\ntitle: literal\n---\nbody\n'):
            self.file.write_bytes(text.encode()); self.c = Public()
            row = self.call('-t', 'Explicit', '-T', str(self.file))
            self.assertEqual(row['title'], 'Explicit'); self.assertEqual(row['body'], text)
            self.assertTrue(row['create_verification']['readback'])
            self.assertEqual(len(self.c.writes()), 1)

    def test_editor_seed_replacement_and_title_from_editor(self):
        with patch('gitverse_cli.comment_editor.edit_text', return_value='Edited\nReplaced') as edit:
            row = self.call('-T', str(self.file), '-e')
        self.assertEqual(edit.call_args.args[0], '\n'+self.text)
        self.assertEqual((row['title'], row['body']), ('Edited', 'Replaced'))

    def test_draft_metadata_and_maintainer_combination(self):
        self.c.pull['maintainer_can_modify'] = False
        row = self.create('-d', '-a', '@me', '-l', 'one', '--no-maintainer-edit')
        self.assertEqual(row['body'], self.text); self.assertEqual(row['title'], 'Draft: Template')
        self.assertEqual(row['assignees'][0]['id'], 1); self.assertEqual(row['labels'][0]['id'], 11)
        self.assertIs(row['maintainer_can_modify'], False)

    def test_explicit_body_conflicts_even_empty_before_input_or_editor(self):
        for flags in (['-b', ''], ['--body', 'text'], ['-F', '-'], ['--body-file', 'missing']):
            with patch('sys.stdin', io.StringIO('never')), patch('gitverse_cli.comment_editor.edit_text', side_effect=AssertionError('editor')):
                with self.assertRaises(GVError) as caught: self.create('-e', *flags)
            self.assertEqual(caught.exception.code, 'usage_error'); self.assertEqual(self.c.calls, [])

    def test_title_required_without_editor_or_fill(self):
        with self.assertRaises(GVError): self.call('-T', str(self.file))
        self.assertEqual(self.c.calls, [])

    def test_invalid_paths_do_not_read_stdin_or_http(self):
        link = self.file.parent/'link'; link.symlink_to(self.file)
        fifo = self.file.parent/'fifo'; os.mkfifo(fifo)
        for path in ('', '-', str(link), str(fifo), str(self.file.parent), str(self.file.parent/'missing')):
            with patch('sys.stdin', io.StringIO('never')):
                with self.assertRaises(GVError): self.call('-t', 'T', '-T', path)
            self.assertEqual(self.c.calls, [])

    def test_invalid_content_and_size_bound(self):
        for data in (b'\xff', b'\0', b'x'*1048577):
            self.file.write_bytes(data)
            with self.assertRaises(GVError): self.create()
            self.assertEqual(self.c.calls, [])
        self.file.write_bytes(b'x'*1048576)
        self.assertEqual(len(self.create()['body']), 1048576)

    def test_changed_file_refuses_before_network(self):
        original = os.fstat; count = 0
        def changed(fd):
            nonlocal count
            count += 1
            if count == 2: self.file.write_bytes(b'changed')
            return original(fd)
        with patch('gitverse_cli.actions_config.os.fstat', side_effect=changed):
            with self.assertRaises(GVError) as caught: self.create()
        self.assertEqual(caught.exception.code, 'concurrent_change'); self.assertEqual(self.c.calls, [])

    def test_deadline_before_http(self):
        with patch('gitverse_cli.pr_create.time.monotonic', side_effect=[0, 100]):
            with self.assertRaises(GVError) as caught: self.create()
        self.assertEqual(caught.exception.code, 'deadline_exceeded'); self.assertEqual(self.c.calls, [])

    def test_offline_no_auth_network_editor_or_prompt(self):
        self.c = Client(dry_run=True)
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')), patch('socket.socket.connect', side_effect=AssertionError('socket')), patch('gitverse_cli.comment_editor.edit_text', side_effect=AssertionError('editor')), patch('builtins.input', side_effect=AssertionError('prompt')):
            row = self.create('--dry-run')
            self.assertEqual(row['body']['body'], self.text)
            self.assertTrue(row['validation']['final_request_known'])
            self.assertEqual(row['network_requests'], 0)
            row = self.create('--dry-run', '-e', '-a', '@me', '-l', 'one')
            self.assertEqual(row['editor']['seed']['body'], self.text)
            self.assertNotIn('body', row['base_request_template']['body'])
            self.assertFalse(row['validation']['final_request_known'])

    def test_fill_direct_ignores_template_editor_uses_template(self):
        for flag in ('--fill', '--fill-first', '--fill-verbose'):
            self.c = Client(dry_run=True)
            with patch('gitverse_cli.pr_create_fill.prepare', return_value=('Filled', 'Commit body', {'mode': flag})):
                row = self.call(flag, '-T', 'missing', '--dry-run')
                self.assertEqual(row['body']['body'], 'Commit body')
                self.assertFalse(row['template']['applied'])
                row = self.call(flag, '-T', str(self.file), '-e', '--dry-run')
                self.assertEqual(row['editor']['seed'], {'title': 'Filled', 'body': self.text})

    def test_malformed_dto_wrong_author_scope_text_retains_ids(self):
        for changes in ({'body': None}, {'body': 'wrong'}, {'user': {'id': 2, 'login': 'other'}}, {'head': {'ref': 'feature', 'sha': 'a'*40, 'repo': {'id': 99, 'full_name': 'other/repo'}}}):
            self.c = Public(); self.intercept(lambda m,p,r,kw: {**r, **changes} if m=='POST' else r)
            with self.assertRaises(GVError) as caught: self.create()
            self.assertEqual(caught.exception.details['pull_id'], 88)
            self.assertEqual(len(self.c.writes()), 1)

    def test_concurrent_readback_and_repeat_is_new_creation(self):
        self.c.on_read = lambda c,n: c.pull.update(body='concurrent')
        with self.assertRaises(GVError) as caught: self.create()
        self.assertEqual(caught.exception.details['pull_number'], 8)
        self.assertEqual(len(self.c.writes()), 1)
        self.c = Public(); self.create(); self.create()
        self.assertEqual(len(self.c.writes()), 2)

    def test_denials_uncertain_mutation_and_transport_bounds_never_retry(self):
        for stage, code, status in [('preflight','http_error',401), ('preflight','http_error',403), ('create','http_error',403), ('create','network_error',None), ('readback','response_limit',None), ('readback','deadline_exceeded',None), ('readback','incomplete_response',None)]:
            self.c = Public(); original = self.c.request
            def request(m,p,q=None,body=None,**kw):
                if (stage=='preflight' and m=='GET') or (stage=='create' and m=='POST') or (stage=='readback' and p.endswith('/pulls/8')):
                    self.c.calls.append((m,p,q,body)); raise GVError('Failure',code,status)
                return original(m,p,q,body,**kw)
            self.c.request = request
            with self.assertRaises(GVError) as caught: self.create()
            self.assertEqual(len(self.c.writes()), int(stage!='preflight'))
            self.assertEqual(caught.exception.details['mutation_outcome'], 'none' if stage=='preflight' else 'unknown' if stage=='create' else 'confirmed')

    def test_redirect_never_forwards_credentials(self):
        import urllib.error
        from unittest.mock import Mock
        with patch.dict(os.environ, {'GITVERSE_TOKEN': 'synthetic-only-token'}):
            self.c = Client()
        self.c.opener = Mock()
        self.c.opener.open.side_effect = urllib.error.HTTPError('https://api.gitverse.ru/repos/owner/repo', 302, 'redirect', {'Location': 'https://foreign.invalid/'}, None)
        with self.assertRaises(GVError): self.create()
        self.assertEqual(self.c.opener.open.call_count, 1)
        request = self.c.opener.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith('https://api.gitverse.ru/'))
        self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-only-token')
        self.assertIsNone(request.get_header('Cookie'))

    def test_saved_failure_contains_template_body_without_path_and_explicit_template_overrides(self):
        original = self.c.request
        def fail(m,p,*a,**kw):
            if m=='GET': raise GVError('Denied', 'http_error', 401)
            return original(m,p,*a,**kw)
        self.c.request = fail
        with self.assertRaises(GVError) as caught: self.create()
        saved = Path(caught.exception.details['recover_file']); self.addCleanup(saved.unlink)
        import json
        data = json.loads(saved.read_text())
        self.assertEqual(data['input']['body'], self.text)
        self.assertNotIn('template', data['input'])
        self.file.write_text('New template'); self.c = Client(dry_run=True)
        row = self.call('--recover', str(saved), '-T', str(self.file), '--dry-run')
        self.assertEqual(row['body']['body'], 'New template')

    def test_discovery_and_missing_argument(self):
        self.assertEqual(execute(parser().parse_args(['agent-info']))['pr_create_template']['flags'], ['--template', '-T'])
        with self.assertRaises(GVError): self.call('-T')


class CatalogTests(unittest.TestCase):
    setUp = TemplateTests.setUp
    call = TemplateTests.call
    def setup_catalog(self, path='.github/PULL_REQUEST_TEMPLATE/change.md', text=None):
        import base64, hashlib
        self.catalog_text = self.text if text is None else text
        self.catalog_commit = 'e'*40
        blob = self.catalog_text.encode()
        digest = hashlib.sha1(b'blob '+str(len(blob)).encode()+b'\0'+blob).hexdigest()
        parts = path.split('/')
        self.rows = [{'path':'/'.join(parts[:i]),'type':'tree','mode':'040000','sha':'f'*40} for i in range(1,len(parts))]
        self.rows.append({'path':path,'type':'blob','mode':'100644','sha':digest,'size':len(blob)})
        self.blob = {'path':path,'type':'file','sha':digest,'size':len(blob),'encoding':'base64','content':base64.b64encode(blob).decode()}
        original = self.c.request
        def request(m,p,q=None,body=None,**kw):
            if '/git/trees/' in p:
                self.c.calls.append((m,p,q,body))
                return {'sha':self.catalog_commit,'tree':self.rows,'page':q['page'],'total_count':len(self.rows),'truncated':False}
            if '/commits/' in p:
                self.c.calls.append((m,p,q,body)); return {'sha':self.catalog_commit}
            if '/contents/' in p:
                self.c.calls.append((m,p,q,body)); self.assertEqual(q['ref'],self.catalog_commit); return self.blob
            return original(m,p,q,body,**kw)
        self.c.request = request

    def catalog(self,*flags):
        return self.call('-t','Catalog','-T','change.md',*flags)

    def test_catalog_normal_precedence_and_editor_metadata(self):
        for parent in ('.github', '', 'docs'):
            self.c=Public(); self.setup_catalog('/'.join(p for p in (parent,'pull_request_template/change.md') if p))
            row=self.catalog(); self.assertEqual(row['body'],self.text); self.assertEqual(len(self.c.writes()),1)
        self.c=Public(); self.setup_catalog()
        with patch('gitverse_cli.comment_editor.edit_text',return_value='Edited\nNew') as edit:
            row=self.catalog('-e','-d','-a','@me')
        self.assertEqual(edit.call_args.args[0],'Catalog\n'+self.text)
        self.assertEqual(row['body'],'New'); self.assertEqual(row['title'],'Draft: Edited')

    def test_catalog_offline_is_unresolved(self):
        self.c=Client(dry_run=True)
        with patch('socket.socket.connect',side_effect=AssertionError('network')):
            row=self.catalog('--dry-run')
        self.assertFalse(row['validation']['final_request_known']); self.assertFalse(row['template']['resolved'])
        self.assertFalse(row['template']['body_known']); self.assertNotIn('body',row['body'])
        self.assertEqual(row['network_requests'],0)

    def test_catalog_name_invalid_and_missing(self):
        for name in ('bad\x85.md','bad\n.md','x'*1025):
            self.c=Public()
            with self.assertRaises(GVError):self.call('-t','T','-T',name)
            self.assertEqual(self.c.calls,[])
        self.setup_catalog(path='docs/PULL_REQUEST_TEMPLATE/other.md')
        with self.assertRaises(GVError) as caught:self.catalog()
        self.assertEqual(caught.exception.code,'template_not_found');self.assertEqual(self.c.writes(),[])

    def test_catalog_malformed_blob_and_tree_refuse_without_write(self):
        for field,value in (('sha','0'*40),('size',99),('content','%%%'),('path','other.md'),('type','symlink')):
            self.c=Public(); self.setup_catalog();self.blob[field]=value
            with self.assertRaises(GVError):self.catalog()
            self.assertEqual(self.c.writes(),[])
        for mode in ('120000','160000'):
            self.c=Public();self.setup_catalog();self.rows[-1].update(mode=mode,type='blob' if mode=='120000' else 'commit')
            with self.assertRaises(GVError):self.catalog()
            self.assertEqual(self.c.writes(),[])
        self.c=Public(); self.setup_catalog();self.rows.append(dict(self.rows[-1]))
        with self.assertRaises(GVError):self.catalog()
        self.assertEqual(self.c.writes(),[])

    def test_catalog_limits_denial_and_incomplete_tree(self):
        from types import SimpleNamespace
        for code in ('pagination_limit','contents_limit','deadline_exceeded','response_limit','incomplete_response'):
            self.c=Public();self.setup_catalog()
            with patch('gitverse_cli.pr_create_template.tree',side_effect=GVError('Bound',code)):
                with self.assertRaises(GVError) as caught:self.catalog()
            self.assertEqual(caught.exception.code,code);self.assertEqual(self.c.writes(),[])
        self.c=Public();self.setup_catalog()
        with patch('gitverse_cli.pr_create_template.LIMITS',SimpleNamespace(max_pages=0,page_size=100,max_entries=100000)):
            with self.assertRaises(GVError) as caught:self.catalog()
        self.assertEqual(caught.exception.code,'pagination_limit')
        for status in (401,403):
            self.c=Public();self.setup_catalog()
            with patch('gitverse_cli.pr_create_template.tree',side_effect=GVError('Denied','http_error',status)):
                with self.assertRaises(GVError) as caught:self.catalog()
            self.assertFalse(caught.exception.details['mutation_attempted'])
            self.assertEqual(caught.exception.details['mutation_outcome'],'none')
            self.assertEqual(self.c.writes(),[])

    def test_catalog_repo_replacement_refuses_and_unknown_post_keeps_recovery(self):
        self.setup_catalog();original=self.c.request;count=0
        def changed(m,p,q=None,body=None,**kw):
            nonlocal count
            r=original(m,p,q,body,**kw)
            if p=='repos/'+REPO:
                count+=1
                if count>1:r={**r,'id':9999}
            return r
        self.c.request=changed
        with self.assertRaises(GVError):self.catalog()
        self.assertEqual(self.c.writes(),[])
        self.c=Public();self.setup_catalog();original=self.c.request
        def uncertain(m,p,q=None,body=None,**kw):
            if m=='POST':
                self.c.calls.append((m,p,q,body));raise GVError('Interrupted','network_error')
            return original(m,p,q,body,**kw)
        self.c.request=uncertain
        with self.assertRaises(GVError) as caught:self.catalog()
        self.assertEqual(caught.exception.details['mutation_outcome'],'unknown');self.assertEqual(len(self.c.writes()),1)

    def test_catalog_malformed_default_branch(self):
        for value in (None, '', 1, [], {}):
            self.c=Public();self.setup_catalog();original=self.c.request
            def malformed(m,p,q=None,body=None,**kw):
                r=original(m,p,q,body,**kw)
                return {**r,'default_branch':value} if p=='repos/'+REPO else r
            self.c.request=malformed
            with self.assertRaises(GVError) as caught:self.catalog()
            self.assertEqual(caught.exception.code,'unexpected_response')
            self.assertEqual(self.c.writes(),[])

    def test_catalog_interrupted_transport_is_json_error_before_write(self):
        import http.client
        self.setup_catalog()
        with patch('gitverse_cli.pr_create_template.tree',side_effect=http.client.IncompleteRead(b'',1)):
            with self.assertRaises(GVError) as caught:self.catalog()
        self.assertEqual(caught.exception.code,'network_error')
        self.assertEqual(caught.exception.details['mutation_outcome'],'none')
        self.assertEqual(self.c.writes(),[])
