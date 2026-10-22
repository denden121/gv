"""Literal create body contracts; no remote API calls."""
import copy
import io
import unittest
from unittest.mock import patch

import test_pr_create as core
from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from test_metadata import Public


class BodyTests(unittest.TestCase):
    setUp = core.CreateTests.setUp
    create = core.CreateTests.create
    intercept = core.CreateTests.intercept

    def test_long_short_literal_preserves_text_without_input_or_editor(self):
        text = '  Текст 😀\r\n# Markdown\n`$HOME` $(echo literal) ![x](./file)\n  '
        for flag in ('--body', '-b'):
            self.c = Public()
            with patch('builtins.input', side_effect=AssertionError('prompt')), patch('subprocess.run', side_effect=AssertionError('process')), patch('sys.stdin', None):
                row = self.create(flag, text)
            self.assertEqual(row['body'], text)
            self.assertEqual(self.c.writes()[0][3]['body'], text)
            self.assertEqual(len(self.c.writes()), 1)
            self.assertEqual(self.c.calls[-1][0], 'GET')

    def test_empty_whitespace_and_omission_are_distinct_payloads(self):
        for value in ('', ' \t\r\n'):
            self.c = Public()
            self.assertEqual(self.create('--body', value)['body'], value)
            self.assertIn('body', self.c.writes()[0][3])
        self.c = Public(); self.create()
        self.assertNotIn('body', self.c.writes()[0][3])

    def test_flag_looking_body_is_literal(self):
        self.assertEqual(self.create('--body=--literal')['body'], '--literal')

    def test_multibyte_byte_bound_inclusive(self):
        self.assertEqual(len(self.create('-b', 'я' * 524288)['body'].encode()), 1048576)
        self.c = Public()
        with self.assertRaises(GVError) as caught: self.create('-b', 'я' * 524289)
        self.assertEqual(caught.exception.code, 'usage_error'); self.assertEqual(self.c.calls, [])

    def test_nul_and_invalid_utf8_refuse_before_network(self):
        for value in ('before\0after', '\udcff', '\ud800'):
            for dry in ((), ('--dry-run',)):
                self.c = Public(); self.c.dry_run = bool(dry)
                with self.assertRaises(GVError) as caught: self.create('--body', value, *dry)
                self.assertEqual(caught.exception.code, 'usage_error'); self.assertEqual(self.c.calls, [])

    def test_body_file_conflict_and_missing_argument_are_json_usage_errors(self):
        for flags in (('--body', 'text', '--body-file', '-'), ('-b',)):
            with self.assertRaises(GVError) as caught: self.create(*flags)
            self.assertEqual(caught.exception.code, 'usage_error')
        self.assertEqual(self.c.calls, [])

    def test_draft_assignee_label_combination_preserves_literal_body(self):
        text = '  описание\r\n'
        row = self.create('-b', text, '--draft', '--assignee', '@me', '--label', 'one')
        self.assertEqual(row['body'], text); self.assertTrue(row['draft'])
        self.assertEqual(len(self.c.writes()), 1)

    def test_offline_literal_and_metadata_plan_no_network_or_input(self):
        self.c = Client(dry_run=True)
        for flags in ((), ('--draft', '--assignee', '@me', '--label', 'one')):
            with patch('socket.socket.connect', side_effect=AssertionError('socket')), patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('HTTP')), patch('subprocess.run', side_effect=AssertionError('process')), patch('builtins.input', side_effect=AssertionError('prompt')), patch('sys.stdin', None):
                row = self.create('--body', '\r\n😀  ', '--dry-run', *flags)
            payload = row.get('base_request_template', row)['body']
            self.assertEqual(payload['body'], '\r\n😀  ')
            self.assertNotIn('number', row)

    def test_malformed_or_normalized_post_body_is_not_success(self):
        for value in (None, {}, [], True, 'trimmed', 'text\n'):
            self.c = Public()
            self.intercept(lambda m,p,r,kw: {**r, 'body':value} if m=='POST' else r)
            with self.assertRaises(GVError) as caught: self.create('-b', ' text\r\n')
            self.assertEqual(caught.exception.details['stage'], 'validate_create')
            self.assertEqual(caught.exception.details['pull_number'], 8)
            self.assertTrue(caught.exception.details['mutation_attempted']); self.assertEqual(len(self.c.writes()), 1)

    def test_concurrent_readback_body_change_preserves_recovery(self):
        self.c.on_read = lambda c,n: c.pull.update(body='parallel change')
        with self.assertRaises(GVError) as caught: self.create('-b', 'requested')
        self.assertEqual(caught.exception.details['stage'], 'verify_create')
        self.assertEqual(caught.exception.details['pull_id'], 88)
        self.assertEqual(len(self.c.writes()), 1)

    def test_wrong_author_and_repository_refuse_with_body(self):
        for change in ({'user':{'id': 2, 'login':'other'}}, {'head':{'ref':'feature', 'sha':'a'*40, 'repo':{'id':99,'full_name':'elsewhere/repo'}}}):
            self.c = Public(); self.intercept(lambda m,p,r,kw: {**r, **change} if m=='POST' else r)
            with self.assertRaises(GVError): self.create('-b', 'literal')
            self.assertEqual(len(self.c.writes()), 1)

    def test_denial_preflight_and_write_are_never_retried(self):
        for stage in ('preflight', 'create'):
            for status in (401,403):
                self.c = Public(); original = self.c.request
                def request(m,p,q=None,body=None,**kw):
                    if (stage=='preflight' and m=='GET') or (stage=='create' and m=='POST'):
                        self.c.calls.append((m,p,q,body)); raise GVError('Denied', 'http_error', status)
                    return original(m,p,q,body,**kw)
                self.c.request = request
                with self.assertRaises(GVError) as caught: self.create('-b', 'literal')
                self.assertEqual(caught.exception.details['mutation_attempted'], stage=='create')
                self.assertEqual(len(self.c.writes()), int(stage=='create'))

    def test_unknown_post_and_readback_failure_no_retry(self):
        for fail_post in (True,False):
            self.c=Public(); original=self.c.request
            def request(m,p,q=None,body=None,**kw):
                if (fail_post and m=='POST') or (not fail_post and p.endswith('/pulls/8')):
                    self.c.calls.append((m,p,q,body)); raise GVError('Interrupted', 'network_error')
                return original(m,p,q,body,**kw)
            self.c.request=request
            with self.assertRaises(GVError) as caught: self.create('-b','literal')
            self.assertTrue(caught.exception.details['mutation_attempted'])
            self.assertEqual(caught.exception.details['pull_number'], None if fail_post else 8)
            self.assertEqual(len(self.c.writes()),1)

    def test_repeated_body_is_new_create_never_noop(self):
        self.create('-b','same'); self.create('-b','same')
        self.assertEqual(len(self.c.writes()),2)

    def test_discovery(self):
        row=execute(parser().parse_args(['agent-info']))
        self.assertEqual(row['pr_create_body']['flags'], ['--body', '-b'])
