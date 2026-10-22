"""PR create file/stdin behavior; synthetic contract transport only."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_pr_create as core
from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from test_metadata import Public


class BodyFileTests(unittest.TestCase):
    create = core.CreateTests.create
    intercept = core.CreateTests.intercept

    def setUp(self):
        core.CreateTests.setUp(self)
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'body.md'
        self.text = '  Текст 😀\r\n# Markdown\n`$HOME` $(literal)\n  '
        self.path.write_bytes(self.text.encode())

    def file_create(self, *flags):
        return self.create('--body-file', str(self.path), *flags)

    def test_long_short_file_and_binary_stdin_preserve_exact_text(self):
        for flag in ('--body-file', '-F'):
            self.c = Public()
            with patch('sys.stdin', None), patch('builtins.input', side_effect=AssertionError('prompt')):
                self.assertEqual(self.create(flag, str(self.path))['body'], self.text)
            self.c = Public()
            with patch('sys.stdin', io.TextIOWrapper(io.BytesIO(self.text.encode()), encoding='utf-8')):
                self.assertEqual(self.create(flag, '-')['body'], self.text)
            self.assertEqual(len(self.c.writes()), 1)
            self.assertEqual(self.c.calls[-1][0], 'GET')

    def test_empty_whitespace_and_bom_files_are_literal(self):
        for text in ('', ' \t\r\n', '\ufeff# Heading\n'):
            self.c = Public(); self.path.write_bytes(text.encode())
            self.assertEqual(self.file_create()['body'], text)
            self.assertIn('body', self.c.writes()[0][3])

    def test_inclusive_multibyte_byte_limit_file_and_stdin(self):
        for data, good in [('я'.encode() * 524288, True), ('я'.encode() * 524289, False)]:
            for stdin in (False, True):
                self.c = Public(); self.path.write_bytes(data)
                with patch('sys.stdin', io.TextIOWrapper(io.BytesIO(data), encoding='utf-8')):
                    if good:
                        row = self.create('-F', '-' if stdin else str(self.path))
                        self.assertEqual(len(row['body'].encode()), 1048576)
                    else:
                        with self.assertRaises(GVError) as caught: self.create('-F', '-' if stdin else str(self.path))
                        self.assertEqual(caught.exception.code, 'value_limit'); self.assertEqual(self.c.calls, [])

    def test_invalid_utf8_and_nul_file_and_stdin_no_network(self):
        for data in (b'\xff', b'x\0y', b'\xf0\x9f'):
            for stdin in (False, True):
                self.c = Public(); self.path.write_bytes(data)
                with patch('sys.stdin', io.TextIOWrapper(io.BytesIO(data), encoding='utf-8')):
                    with self.assertRaises(GVError) as caught: self.create('-F', '-' if stdin else str(self.path))
                self.assertEqual(caught.exception.code, 'usage_error'); self.assertEqual(self.c.calls, [])

    def test_missing_directory_symlink_and_fifo_refuse(self):
        link = self.path.parent/'link'; link.symlink_to(self.path)
        fifo = self.path.parent/'fifo'; os.mkfifo(fifo)
        for path in (self.path.parent/'missing', self.path.parent, link, fifo):
            with self.assertRaises(GVError): self.create('-F', str(path))
            self.assertEqual(self.c.calls, [])

    def test_changed_file_snapshot_refuses_before_network(self):
        original = os.fstat; count = 0
        def changed(fd):
            nonlocal count
            count += 1
            if count == 2: self.path.write_bytes(b'changed')
            return original(fd)
        with patch('gitverse_cli.actions_config.os.fstat', side_effect=changed):
            with self.assertRaises(GVError) as caught: self.file_create()
        self.assertEqual(caught.exception.code, 'concurrent_change'); self.assertEqual(self.c.calls, [])

    def test_tty_stdin_refuses_without_read_or_prompt(self):
        source = io.StringIO(); source.isatty = lambda: True
        source.read = lambda *a: self.fail('TTY read')
        with patch('sys.stdin', source):
            with self.assertRaises(GVError) as caught: self.create('-F', '-')
        self.assertEqual(caught.exception.code, 'usage_error'); self.assertEqual(self.c.calls, [])

    def test_idle_real_pipe_deadline(self):
        root = str(Path(__file__).resolve().parents[1])
        p = subprocess.Popen([sys.executable, '-c', 'from gitverse_cli.cli import main; main()', 'pr', 'create', '-R', 'synthetic/owned', '--head', 'feature', '--base', 'main', '--title', 'test', '-F', '-', '--timeout', '0.05', '--dry-run'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, 'PYTHONPATH': root})
        self.addCleanup(lambda: p.kill() if p.poll() is None else None)
        self.assertEqual(p.wait(timeout=5), 1)
        import json
        self.assertEqual(json.loads(p.stderr.read())['error']['code'], 'deadline_exceeded')
        p.stdin.close(); p.stdout.close(); p.stderr.close()

    def test_exclusive_body_and_missing_argument_refuse(self):
        for flags in (('-F',), ('-F', '-', '-b', 'text')):
            with self.assertRaises(GVError) as caught: self.create(*flags)
            self.assertEqual(caught.exception.code, 'usage_error')
        self.assertEqual(self.c.calls, [])

    def test_offline_file_stdin_metadata_no_network(self):
        self.c = Client(dry_run=True)
        for path in (str(self.path), '-'):
            with patch('sys.stdin', io.StringIO(self.text)), patch('socket.socket.connect', side_effect=AssertionError('socket')), patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('HTTP')), patch('builtins.input', side_effect=AssertionError('prompt')):
                row = self.create('-F', path, '--dry-run', '--assignee', '@me', '--label', 'one')
            self.assertEqual(row['base_request_template']['body']['body'], self.text)
            self.assertNotIn('number', row)

    def test_file_metadata_combination(self):
        row = self.file_create('--assignee', '@me', '--label', 'one')
        self.assertEqual(row['body'], self.text); self.assertEqual(len(self.c.writes()), 1)

    def test_malformed_body_and_wrong_scope_author_preserve_ids(self):
        for changes in ({'body': None}, {'body': self.text.strip()}, {'user': {'id': 2, 'login': 'other'}}, {'head': {'ref':'feature', 'sha':'a'*40, 'repo':{'id':99,'full_name':'elsewhere/repo'}}}):
            self.c = Public(); self.intercept(lambda m,p,r,kw: {**r, **changes} if m=='POST' else r)
            with self.assertRaises(GVError) as caught: self.file_create()
            self.assertEqual(caught.exception.details['pull_id'], 88); self.assertEqual(len(self.c.writes()), 1)

    def test_concurrent_readback_change_no_retry(self):
        self.c.on_read = lambda c,n: c.pull.update(body='changed')
        with self.assertRaises(GVError) as caught: self.file_create()
        self.assertEqual(caught.exception.details['stage'], 'verify_create')
        self.assertEqual(caught.exception.details['pull_number'], 8); self.assertEqual(len(self.c.writes()), 1)

    def test_denials_unknown_post_and_bounded_readback_no_retry(self):
        for stage, code, status in [('preflight','http_error',401), ('preflight','http_error',403), ('create','http_error',403), ('create','network_error',None), ('readback','response_limit',None), ('readback','deadline_exceeded',None), ('readback','incomplete_response',None)]:
            self.c = Public(); original = self.c.request
            def request(m,p,q=None,body=None,**kw):
                if (stage=='preflight' and m=='GET') or (stage=='create' and m=='POST') or (stage=='readback' and p.endswith('/pulls/8')):
                    self.c.calls.append((m,p,q,body)); raise GVError('Failure',code,status)
                return original(m,p,q,body,**kw)
            self.c.request = request
            with self.assertRaises(GVError) as caught: self.file_create()
            self.assertEqual(caught.exception.details['mutation_attempted'], stage!='preflight')
            self.assertEqual(len(self.c.writes()), int(stage!='preflight'))
            self.assertEqual(caught.exception.details['pull_number'], 8 if stage=='readback' else None)

    def test_repeat_same_file_is_new_creation_not_noop(self):
        self.file_create(); self.file_create(); self.assertEqual(len(self.c.writes()), 2)

    def test_discovery(self):
        row = execute(parser().parse_args(['agent-info']))
        self.assertEqual(row['pr_create_body_file']['flags'], ['--body-file', '-F'])
