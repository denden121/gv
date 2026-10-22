"""The body flag supplies literal discussion text; it never invokes an editor."""
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from gitverse_cli.pr_comment import discussion_comment
from test_pr_comment import Fake


class BodyFlagTests(unittest.TestCase):
    def call(self, text, flag='--body', dry_run=False):
        client = Fake()
        client.dry_run = dry_run
        args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', flag, text])
        args.number = 9
        return discussion_comment(client, 'o/r', args), client

    def test_long_short_and_equals_preserve_literal_text(self):
        text = '  **Привет 😀**\r\n$HOME $(literal) `code` \\ "\n\n'
        for flag in ('--body', '-b'):
            with self.subTest(flag=flag):
                row, client = self.call(text, flag)
                self.assertEqual(row['body'], text)
                self.assertEqual([c[3] for c in client.calls if c[0] == 'POST'], [{'body': text}])
        args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', '--body=--literal', '--dry-run'])
        self.assertEqual(execute(args)['body'], {'body': '--literal'})

    def test_exact_utf8_byte_limit_including_multibyte(self):
        for text in ('x' * 1048576, '😀' * 262144):
            result, client = self.call(text, dry_run=True)
            self.assertEqual(result['body']['body'], text)
            self.assertEqual(client.calls, [])
        with self.assertRaises(GVError) as caught:
            self.call('😀' * 262145)
        self.assertEqual(caught.exception.code, 'usage_error')

    def test_invalid_unicode_is_structured_before_network(self):
        for text in ('\udcff', 'valid\ud800text'):
            with patch.object(Fake, 'request', side_effect=AssertionError('network')):
                with self.assertRaises(GVError) as caught: self.call(text)
            self.assertEqual(caught.exception.code, 'usage_error')

    def test_body_and_file_are_mutually_exclusive(self):
        with self.assertRaises(GVError) as caught:
            parser().parse_args(['pr', 'comment', '9', '--body', 'text', '--body-file', 'missing'])
        self.assertEqual(caught.exception.code, 'usage_error')

    def test_missing_blank_and_nul_refuse_without_network(self):
        for flags in ([], ['--body', ''], ['--body', '\r\n\t'], ['--body', 'a\0b']):
            args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', *flags])
            with patch.object(Client, 'request', side_effect=AssertionError('network')):
                with self.assertRaises(GVError) as caught: execute(args)
            self.assertEqual(caught.exception.code, 'usage_error')

    def test_offline_body_does_not_read_stdin_or_launch_process(self):
        with patch('builtins.input', side_effect=AssertionError('prompt')), \
                patch('subprocess.run', side_effect=AssertionError('process')), \
                patch.object(Client, 'request', side_effect=AssertionError('network')):
            args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', '-b', 'literal', '--dry-run'])
            self.assertEqual(execute(args)['body'], {'body': 'literal'})


if __name__ == '__main__':
    unittest.main()
