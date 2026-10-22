"""Behavioral contract for discussion --body-file/-F (no live traffic)."""
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from gitverse_cli.pr_comment import discussion_comment
from test_pr_comment import Fake


class BodyFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'body with spaces.md'
        self.text = '  **Привет 😀**\r\n$HOME $(literal) `code`\r\n\n'
        self.path.write_bytes(self.text.encode())
        self.client = Fake()

    def call(self, flag='--body-file', source=None, *extra):
        args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', flag,
                                   str(self.path) if source is None else source, *extra])
        args.number = 9
        return discussion_comment(self.client, 'o/r', args)

    def writes(self):
        return [c for c in self.client.calls if c[0] != 'GET']

    def test_file_and_short_flag_exact_post_and_independent_read(self):
        for flag in ('--body-file', '-F'):
            self.client = Fake()
            result = self.call(flag)
            self.assertEqual(result['body'], self.text)
            self.assertEqual(self.writes()[0][3], {'body': self.text})
            self.assertEqual(self.client.calls[-1][1], 'repos/o/r/issues/comments/99')

    def test_stdin_binary_exact_and_not_closed(self):
        source = io.TextIOWrapper(io.BytesIO(self.text.encode()), encoding='utf-8')
        with patch('sys.stdin', source):
            self.assertEqual(self.call('-F', '-')['body'], self.text)
        self.assertFalse(source.closed)

    def test_file_does_not_consume_stdin(self):
        with patch('sys.stdin') as stdin:
            stdin.read.side_effect = AssertionError('stdin read')
            stdin.buffer.read.side_effect = AssertionError('stdin read')
            self.assertEqual(self.call()['body'], self.text)

    def test_maximum_is_utf8_bytes_file_and_stdin(self):
        for size in (1048576, 1048578):
            data = ('я' * (size // 2)).encode()
            self.path.write_bytes(data)
            for source in (None, '-'):
                self.client = Fake(); self.client.dry_run = True
                with patch('sys.stdin', io.TextIOWrapper(io.BytesIO(data), encoding='utf-8')):
                    if size == 1048576:
                        self.assertEqual(len(self.call(source=source)['body']['body'].encode()), size)
                    else:
                        with self.assertRaises(GVError) as caught: self.call(source=source)
                        self.assertEqual(caught.exception.code, 'value_limit')
                self.assertEqual(self.client.calls, [])

    def test_blank_binary_nul_refuse_file_and_stdin_before_network(self):
        for data in (b'', b' \r\n\t', b'\xff', b'x\0y'):
            self.path.write_bytes(data)
            for source in (None, '-'):
                with patch('sys.stdin', io.TextIOWrapper(io.BytesIO(data), encoding='utf-8')):
                    with self.assertRaises(GVError): self.call(source=source)
        self.assertEqual(self.client.calls, [])

    def test_tty_refuses_without_prompt(self):
        with patch('sys.stdin') as stdin, patch('builtins.input', side_effect=AssertionError('prompt')):
            stdin.isatty.return_value = True
            with self.assertRaises(GVError) as caught: self.call(source='-')
        self.assertEqual(caught.exception.code, 'usage_error')
        self.assertEqual(self.client.calls, [])

    def test_idle_pipe_stdin_deadline(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        with os.fdopen(read_fd, 'r') as source, patch('sys.stdin', source):
            with self.assertRaises(GVError) as caught: self.call('--body-file', '-', '--timeout', '0.01')
        self.assertEqual(caught.exception.code, 'deadline_exceeded')
        self.assertEqual(self.client.calls, [])

    def test_nonregular_symlink_missing_and_changed_file(self):
        target = self.path.parent / 'link'; target.symlink_to(self.path)
        fifo = self.path.parent / 'fifo'; os.mkfifo(fifo)
        for path in (target, fifo, self.path.parent, self.path.parent / 'missing'):
            with self.assertRaises((GVError, OSError)): self.call(source=str(path))
        actual = os.fstat
        count = 0
        def changed(fd):
            nonlocal count
            before = actual(fd); count += 1
            if count == 2:
                values = list(before); values[6] += 1
                return os.stat_result(values)
            return before
        with patch('os.fstat', side_effect=changed):
            with self.assertRaises(GVError) as caught: self.call()
        self.assertEqual(caught.exception.code, 'concurrent_change')
        self.assertEqual(self.client.calls, [])

    def test_mutually_exclusive_and_offline_preview(self):
        with self.assertRaises(GVError):
            parser().parse_args(['pr', 'comment', '9', '--body', 'text', '-F', str(self.path)])
        with patch.object(Client, 'request', side_effect=AssertionError('network')), \
                patch('subprocess.run', side_effect=AssertionError('process')), \
                patch('builtins.input', side_effect=AssertionError('prompt')):
            args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', '-F', str(self.path), '--dry-run'])
            self.assertEqual(execute(args)['body'], {'body': self.text})

    def test_repeated_file_appends_no_noop(self):
        self.call(); self.call()
        self.assertEqual(len(self.writes()), 2)

    def test_unknown_and_known_id_outcomes_never_retry(self):
        self.client.fail = GVError('timeout', 'network_error')
        with self.assertRaises(GVError) as caught: self.call()
        self.assertTrue(caught.exception.details['mutation_attempted'])
        self.assertNotIn('comment_id', caught.exception.details)
        self.assertEqual(len(self.writes()), 1)
        self.client = Fake()
        def hook(client, method, path):
            if '/issues/comments/' in path: client.rows[-1]['body'] = 'concurrent'
        self.client.hook = hook
        with self.assertRaises(GVError) as caught: self.call()
        self.assertEqual(caught.exception.details['comment_id'], 99)
        self.assertEqual(caught.exception.code, 'verification_failed')
        self.assertEqual(len(self.writes()), 1)

    def test_wrong_identity_permission_and_malformed_result(self):
        for mode in ('parent', 'user', 'denied', 'malformed', 'foreign'):
            self.client = Fake(); original = self.client.request
            if mode == 'denied': self.client.fail = GVError('denied', 'http_error', 403)
            def request(method, path, **kw):
                row = original(method, path, **kw)
                if mode == 'parent' and '/pulls/' in path: row['number'] = 10
                if mode == 'user' and path == 'user': row['id'] = True
                if mode == 'malformed' and method == 'POST': return {'id': 99}
                if mode == 'foreign' and '/issues/comments/' in path: row['issue_url'] = 'https://foreign.invalid/comments/99'
                return row
            self.client.request = request
            with self.assertRaises(GVError): self.call()
            self.assertEqual(len(self.writes()), 0 if mode in ('parent', 'user') else 1)
            self.assertTrue(all('foreign.invalid' not in c[1] for c in self.client.calls))
