"""Behavior of the checks field exporter over the bounded native collector."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from gitverse_cli.checks import JSON_FIELDS, ChecksResult, export_checks, pr_checks
from gitverse_cli.cli import main, parser
from gitverse_cli.client import Client, GVError
from test_checks import FakeClient, REPO, SHA, cycle


class ChecksJSONTests(unittest.TestCase):
    def invoke(self, flags, client=None):
        out, err = io.StringIO(), io.StringIO()
        with patch('gitverse_cli.cli.Client', return_value=client or FakeClient()), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main(['pr', 'checks', '1', '-R', REPO, *flags])
                code = 0
            except SystemExit as e:
                code = e.code
        return code, json.loads(out.getvalue()) if out.getvalue() else None, json.loads(err.getvalue()) if err.getvalue() else None

    def test_selected_fields_array_and_metadata(self):
        code, rows, meta = self.invoke(['--ci-only', '--json', 'name,bucket,state'])
        self.assertEqual(code, 0)
        self.assertEqual(rows, [{'name': 'test.yml', 'bucket': 'pass', 'state': 'success'}, {'name': 'test.yml / verify', 'bucket': 'pass', 'state': 'success'}])
        self.assertEqual(meta['checks_metadata']['head_sha'], SHA)
        self.assertTrue(meta['checks_metadata']['coverage']['scope_complete'])
        self.assertNotIn('checks', meta['checks_metadata'])

    def test_all_fields_native_timestamp_mapping_and_missing_values(self):
        c = cycle()
        c['jobs'][1][0].update(started_at='2026-10-20T01:00:00Z', completed_at='2026-10-20T01:01:00Z')
        code, rows, _ = self.invoke(['--ci-only', '--json', ','.join(JSON_FIELDS)], FakeClient([c]))
        self.assertEqual(code, 0)
        self.assertEqual(set(rows[1]), set(JSON_FIELDS))
        self.assertEqual(rows[1]['startedAt'], '2026-10-20T01:00:00Z')
        self.assertEqual(rows[1]['completedAt'], '2026-10-20T01:01:00Z')
        self.assertIsNone(rows[0]['startedAt'])
        self.assertIsNone(rows[1]['description'])
        self.assertEqual(rows[1]['link'], 'https://gitverse.ru/owner/repo/cicd/1')

    def test_native_status_buckets_and_exit_codes_preserved(self):
        for state, bucket, code in [('failure', 'fail', 1), ('cancelled', 'cancel', 1), ('running', 'pending', 8), ('neutral', 'skipping', 0), ('future', 'unknown', 3)]:
            with self.subTest(state=state):
                actual, rows, meta = self.invoke(['--ci-only', '--json=bucket,state'], FakeClient([cycle(state, state)]))
                self.assertEqual(actual, code)
                self.assertTrue(all(r == {'bucket': bucket, 'state': state} for r in rows))
                self.assertEqual(meta['checks_metadata']['exit_code'], code)

    def test_unknown_scope_empty_results_and_pagination_are_explicit(self):
        code, _, meta = self.invoke(['--json', 'name'])
        self.assertEqual(code, 3)
        self.assertFalse(meta['checks_metadata']['coverage']['scope_complete'])
        code, rows, _ = self.invoke(['--ci-only', '--json', 'name'], FakeClient([{'runs': []}]))
        self.assertEqual((code, rows), (4, []))
        code, rows, meta = self.invoke(['--ci-only', '--json', 'name', '--max-pages', '1'])
        self.assertEqual(code, 3)
        self.assertTrue(rows)
        self.assertFalse(meta['checks_metadata']['coverage']['ci_scan_complete'])
        self.assertIn('pagination_limit', meta['checks_metadata']['scan_issues'])

    def test_head_change_remains_visible(self):
        c = cycle(); c['head_after'] = 'b' * 40
        code, _, meta = self.invoke(['--ci-only', '--json', 'bucket'], FakeClient([c]))
        self.assertEqual(code, 3)
        self.assertTrue(meta['checks_metadata']['head_changed'])

    def test_invalid_export_options_refuse_before_client_or_network(self):
        for flags in [['--json', ''], ['--json', 'name,'], ['--json', 'head_sha'], ['--json', 'name', '--watch'], ['--json', 'name', '--fields', 'name']]:
            with self.subTest(flags=flags), patch('gitverse_cli.cli.Client', side_effect=AssertionError('client must not be created')), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(['pr', 'checks', '1', '-R', REPO, *flags])
                self.assertEqual(error.exception.code, 2)

    def test_bare_compatibility_and_other_commands_unchanged(self):
        code, envelope, err = self.invoke(['--ci-only', '--json'])
        self.assertEqual(code, 0)
        self.assertIn('checks', envelope)
        self.assertIsNone(err)
        self.assertIs(parser().parse_args(['pr', 'view', '1', '--json']).json, True)
        self.assertIs(parser().parse_args(['--json', 'pr', 'checks', '1']).json, True)

    def test_duplicate_fields_are_deduplicated(self):
        self.assertEqual(export_checks({'checks': [{'name': 'x'}]}, 'name,name'), [{'name': 'x'}])

    def test_offline_preview_retains_plan_and_fields(self):
        args = parser().parse_args(['pr', 'checks', '1', '-R', REPO, '--json', 'name,bucket', '--dry-run'])
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('offline')):
            result = pr_checks(args, Client(dry_run=True), REPO, 1)
        self.assertEqual(result.payload['json_fields'], ['name', 'bucket'])
        self.assertIn('plan', result.payload)

    def test_malformed_timestamp_refuses(self):
        c = cycle(); c['jobs'][1][0]['started_at'] = {}
        code, rows, err = self.invoke(['--ci-only', '--json', 'startedAt'], FakeClient([c]))
        self.assertEqual(code, 1)
        self.assertIsNone(rows)
        self.assertEqual(err['error']['code'], 'unexpected_response')

    def test_malformed_job_name_or_state_refuses(self):
        for field in ('name', 'status'):
            c = cycle(); c['jobs'][1][0][field] = {'malformed': True}
            code, rows, err = self.invoke(['--ci-only', '--json', 'name,state'], FakeClient([c]))
            self.assertEqual(code, 1)
            self.assertIsNone(rows)
            self.assertEqual(err['error']['code'], 'unexpected_response')

    def test_wrong_job_scope_does_not_export_success(self):
        c = cycle(); c['jobs'][1][0]['run_id'] = 999
        code, rows, meta = self.invoke(['--ci-only', '--json', 'bucket'], FakeClient([c]))
        self.assertEqual(code, 3)
        self.assertEqual(rows[-1]['bucket'], 'unknown')
        self.assertFalse(meta['checks_metadata']['coverage']['scope_complete'])

    def test_permission_error_remains_error(self):
        client = FakeClient()
        client.request = lambda *a, **kw: (_ for _ in ()).throw(GVError('Denied', 'http_error', status=403))
        code, rows, err = self.invoke(['--json', 'name'], client)
        self.assertEqual(code, 1)
        self.assertIsNone(rows)
        self.assertEqual(err['error']['status'], 403)
