"""Draft creation behavior; synthetic contracts, never live requests."""
import unittest
from unittest.mock import patch

import test_pr_create as core
from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from test_metadata import Public


class DraftTests(unittest.TestCase):
    setUp = core.CreateTests.setUp
    create = core.CreateTests.create
    intercept = core.CreateTests.intercept

    def test_long_and_short_send_explicit_native_title_and_verify(self):
        for flag in ('--draft', '-d'):
            self.c = Public()
            row = self.create(flag, '--title', 'Описание 😀')
            self.assertEqual(row['title'], 'Draft: Описание 😀')
            self.assertIs(row['draft'], True)
            self.assertEqual(self.c.writes()[0][3]['title'], row['title'])
            self.assertIs(self.c.writes()[0][3]['draft'], True)
            self.assertEqual(len(self.c.writes()), 1)
            self.assertTrue(row['create_verification']['readback'])
            self.assertTrue(self.c.calls[-1][1].endswith('/pulls/8'))

    def test_existing_marker_is_not_duplicated_or_rewritten(self):
        for title in ('Draft: Change', 'draft: Change', 'DRAFT : Change', 'Draft:\tChange'):
            self.c = Public()
            self.assertEqual(self.create('-d', '--title', title)['title'], title)
        self.c = Public()
        self.assertEqual(self.create('-d', '--title', 'Drafting change')['title'], 'Draft: Drafting change')

    def test_omission_retains_normal_creation(self):
        row = self.create()
        self.assertIs(row['draft'], False)
        self.assertEqual(row['title'], 'Created')

    def test_body_file_assignee_label_combinations(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            f = Path(d)/'description.md'; f.write_bytes(' Тело 😀\r\n'.encode())
            row = self.create('-d', '-F', str(f), '-a', '@me', '-l', 'one')
        self.assertEqual(row['body'], ' Тело 😀\r\n')
        self.assertEqual(row['title'], 'Draft: Created')
        self.assertIs(row['draft'], True)
        self.assertEqual(row['assignees'][0]['id'], 1)
        self.assertEqual(len(self.c.writes()), 1)

    def test_blank_invalid_and_normalized_size_bounds_before_network(self):
        for title in ('', ' \t', 'bad\nline', 'bad\rline', 'bad\0line', 'x'*1048570):
            self.c = Public()
            with self.assertRaises(GVError) as e: self.create('-d', '--title', title)
            self.assertEqual(e.exception.code, 'usage_error')
            self.assertEqual(self.c.calls, [])
        row = self.create('-d', '--title', 'x'*1048569)
        self.assertEqual(len(row['title'].encode()), 1048576)

    def test_offline_exact_normalization_and_unresolved_metadata(self):
        self.c = Client(dry_run=True)
        for flags in (('--draft',), ('-d', '--assignee', '@me', '--label', 'one')):
            with patch('socket.socket.connect', side_effect=AssertionError('socket')), patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('HTTP')), patch('builtins.input', side_effect=AssertionError('prompt')), patch('subprocess.run', side_effect=AssertionError('process')):
                row = self.create('--dry-run', *flags)
            payload = row.get('base_request_template', row)['body']
            self.assertEqual(payload['title'], 'Draft: Created')
            self.assertIs(payload['draft'], True)
            self.assertNotIn('number', row)

    def test_malformed_draft_and_unexpected_title_post_are_errors_with_ids(self):
        for field, value in [('draft', None), ('draft', 1), ('draft', 'true'), ('draft', False), ('title', 'Created'), ('title', 'Draft: Other'), ('title', 'Draft: Draft: Created'), ('title', {})]:
            self.c = Public()
            self.intercept(lambda m,p,r,kw: {**r, field:value} if m=='POST' else r)
            with self.assertRaises(GVError) as e: self.create('--draft')
            self.assertEqual(e.exception.details['stage'], 'validate_create')
            self.assertEqual(e.exception.details['pull_id'], 88)
            self.assertEqual(e.exception.details['pull_number'], 8)
            self.assertEqual(len(self.c.writes()), 1)

    def test_readback_draft_title_or_identity_change_refuses(self):
        for field, value in [('draft', False), ('title', 'Draft: changed'), ('id', 89), ('user', {'id':2,'login':'other'}), ('head', {'ref':'feature','sha':'a'*40,'repo':{'id':99,'full_name':'other/repo'}})]:
            self.c = Public()
            self.c.on_read = lambda c,n: c.pull.update({field:value})
            with self.assertRaises(GVError) as e: self.create('-d')
            self.assertEqual(e.exception.details['stage'], 'verify_create')
            self.assertEqual(e.exception.details['pull_id'], 88)
            self.assertEqual(len(self.c.writes()), 1)

    def test_denial_before_or_during_write_no_retry(self):
        for fail_post in (False, True):
            for status in (401,403):
                self.c=Public(); original=self.c.request
                def request(m,p,q=None,body=None,**kw):
                    if (fail_post and m=='POST') or (not fail_post and m=='GET'):
                        self.c.calls.append((m,p,q,body)); raise GVError('Denied','http_error',status)
                    return original(m,p,q,body,**kw)
                self.c.request=request
                with self.assertRaises(GVError) as e: self.create('-d')
                self.assertEqual(e.exception.details['mutation_attempted'], fail_post)
                self.assertEqual(len(self.c.writes()), int(fail_post))

    def test_unknown_post_or_bounded_readback_retains_recovery_no_retry(self):
        for code in ('network_error','response_limit','deadline_exceeded','incomplete_response'):
            for fail_post in (True,False):
                self.c=Public(); original=self.c.request
                def request(m,p,q=None,body=None,**kw):
                    if (fail_post and m=='POST') or (not fail_post and p.endswith('/pulls/8')):
                        self.c.calls.append((m,p,q,body)); raise GVError('Interrupted',code)
                    return original(m,p,q,body,**kw)
                self.c.request=request
                with self.assertRaises(GVError) as e: self.create('-d')
                self.assertTrue(e.exception.details['mutation_attempted'])
                self.assertEqual(e.exception.details['pull_number'], None if fail_post else 8)
                self.assertEqual(e.exception.details['head'], 'feature')
                self.assertEqual(len(self.c.writes()), 1)

    def test_repeated_draft_is_new_creation_never_noop(self):
        self.create('-d'); self.create('-d')
        self.assertEqual(len(self.c.writes()), 2)

    def test_discovery(self):
        self.assertEqual(execute(parser().parse_args(['agent-info']))['pr_create_draft']['flags'], ['--draft','-d'])
