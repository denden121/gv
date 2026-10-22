import copy
import unittest
from unittest.mock import Mock, patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.run_logs import run_logs, jobs_for_run


class RunLogsTests(unittest.TestCase):
    def setUp(self):
        self.args = parser().parse_args(['run', 'view', '7', '-R', 'o/r', '--log'])
        self.run = {'id': 7, 'status': 'failure', 'started_at': 'a', 'completed_at': 'b'}
        self.job = {'id': 9, 'run_id': 7, 'status': 'failure', 'steps': [
            {'name': 'setup', 'number': 0, 'status': 'success', 'log_index': 0, 'log_length': 1},
            {'name': 'test', 'number': 1, 'status': 'failure', 'log_index': 1, 'log_length': 2},
            {'name': 'upload', 'number': 2, 'status': 'success', 'log_index': 3, 'log_length': 1}]}
        self.text = 'setup\r\nошибка  \r\n$(literal)\nlast'
        self.client = Mock(dry_run=False)

    def responses(self, **changes):
        job = {**self.job, **changes}
        self.client.request.side_effect = [self.run, {'total_count': 1, 'jobs': [job]}, self.text, job, self.run]

    def call(self):
        return run_logs(self.client, 'o/r', self.args)

    def test_full_text_preserves_unicode_whitespace_and_line_endings(self):
        self.responses()
        result = self.call()
        self.assertEqual(result['jobs'][0]['log'], self.text)
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual(result['jobs'][0]['line_count'], 4)

    def test_failed_steps_exact_ranges_exclude_success(self):
        self.args.log = False; self.args.log_failed = True; self.responses()
        result = self.call()['jobs'][0]
        self.assertNotIn('log', result)
        self.assertEqual(result['steps'][0]['log'], 'ошибка  \r\n$(literal)\n')
        self.assertEqual(len(result['steps']), 1)

    def test_bare_cr_progress_is_not_a_new_log_record(self):
        self.args.log_failed = True
        self.text = 'setup\nprogress\rfailed\nnext\nlast'
        self.responses()
        self.assertEqual(self.call()['jobs'][0]['steps'][0]['log'], 'progress\rfailed\nnext\n')

    def test_no_failed_steps_does_not_fetch_logs(self):
        self.args.log_failed = True
        job = {**self.job, 'status': 'success', 'steps': []}
        self.client.request.side_effect = [self.run, {'total_count': 1, 'jobs': [job]}, self.run]
        result = self.call()
        self.assertEqual(result['empty_reason'], 'no_failed_steps')
        self.assertEqual(result['jobs'], [])
        self.assertEqual(self.client.request.call_count, 3)

    def test_job_membership_checked_before_logs(self):
        self.args.job = 9
        self.client.request.side_effect = [self.run, {**self.job, 'run_id': 8}]
        with self.assertRaises(GVError) as error: self.call()
        self.assertEqual(error.exception.code, 'job_run_mismatch')
        self.assertEqual(self.client.request.call_count, 2)

    def test_running_or_unknown_run_refuses_logs(self):
        for state in ('running', 'future-status'):
            self.client.request.side_effect = [{**self.run, 'status': state}]
            with self.assertRaises(GVError) as error: self.call()
            self.assertEqual(error.exception.code, 'logs_not_ready')

    def test_rerun_reusing_job_id_refuses_result(self):
        self.responses()
        self.client.request.side_effect = [self.run, {'total_count': 1, 'jobs': [self.job]}, self.text, {**self.job, 'started_at': 'new'}]
        with self.assertRaises(GVError) as error: self.call()
        self.assertEqual(error.exception.code, 'ci_attempt_changed')

    def test_run_rerun_during_read_refuses_result(self):
        self.client.request.side_effect = [self.run, {'total_count': 1, 'jobs': [self.job]}, self.text, self.job, {**self.run, 'completed_at': 'new'}]
        with self.assertRaises(GVError) as error: self.call()
        self.assertEqual(error.exception.code, 'ci_attempt_changed')

    def test_real_run_started_stopped_fields_detect_attempt_change(self):
        self.client.request.side_effect = [{**self.run, 'started': 100}, {'total_count': 1, 'jobs': [self.job]}, self.text, self.job, {**self.run, 'started': 200}]
        with self.assertRaises(GVError) as error: self.call()
        self.assertEqual(error.exception.code, 'ci_attempt_changed')

    def test_missing_logs_explicit_error_without_payload(self):
        self.client.request.side_effect = [self.run, {'total_count': 1, 'jobs': [self.job]}, GVError('missing', 'http_error', 404)]
        with self.assertRaises(GVError) as error: self.call()
        self.assertEqual(error.exception.code, 'logs_unavailable')
        self.assertEqual(error.exception.details, {'run_id': 7, 'job_id': 9})

    def test_truncated_extra_html_or_none_logs_refused(self):
        for text in ('one\ntwo', self.text + '\nextra', '<html>login', None):
            self.client.request.side_effect = [self.run, {'total_count': 1, 'jobs': [self.job]}, text]
            with self.assertRaises(GVError): self.call()

    def test_invalid_step_ranges_and_status_refused(self):
        for changes in ({'log_index': 2}, {'log_length': -1}, {'log_length': True}, {'status': 'future'}, {'status': 'running'}):
            job = copy.deepcopy(self.job); job['steps'][1].update(changes)
            self.client.request.side_effect = [self.run, {'total_count': 1, 'jobs': [job]}]
            with self.assertRaises(GVError): self.call()

    def test_failed_job_without_failed_step_is_not_empty_success(self):
        self.args.log_failed = True
        self.responses(steps=[])
        with self.assertRaises(GVError) as error: self.call()
        self.assertEqual(error.exception.code, 'failed_steps_unavailable')

    def test_pagination_honors_total_despite_server_cap(self):
        other = {**self.job, 'id': 10}
        self.client.request.side_effect = [{'total_count': 2, 'jobs': [self.job]}, {'total_count': 2, 'jobs': [other]}]
        self.assertEqual(len(jobs_for_run(self.client, 'p', 7, 3, 100)), 2)

    def test_pagination_without_total_reads_until_empty(self):
        self.client.request.side_effect = [[self.job], []]
        self.assertEqual(len(jobs_for_run(self.client, 'p', 7, 3, 100)), 1)

    def test_incomplete_repeated_shifting_and_bounded_pages_rejected(self):
        for pages, maximum in (([{'total_count': 2, 'jobs': [self.job]}, {'total_count': 2, 'jobs': []}], 2),
                               ([[self.job], [self.job]], 2),
                               ([{'total_count': 2, 'jobs': [self.job]}, {'total_count': 3, 'jobs': []}], 2),
                               ([[self.job]], 1)):
            self.client.request.side_effect = pages
            with self.assertRaises(GVError): jobs_for_run(self.client, 'p', 7, maximum, 100)

    def test_cli_flags_validation_and_dry_run(self):
        for options in (['--log', '--log-failed'], ['--job', '0'], ['--timeout', 'nan']):
            with self.assertRaises(GVError): parser().parse_args(['run', 'view', '7', *options])
        with patch('gitverse_cli.cli.Client') as factory:
            factory.return_value.backend = 'public'
            with self.assertRaises(GVError): execute(parser().parse_args(['run', 'view', '7', '-R', 'o/r', '--job', '9']))
            factory.return_value.request.assert_not_called()
        result = execute(parser().parse_args(['run', 'view', '7', '-R', 'o/r', '--log-failed', '--job', '9', '--dry-run']))
        self.assertEqual(result['mode'], 'failed_steps')
        self.assertTrue(result['jobs_request']['url'].endswith('/actions/jobs/9'))

    def test_deadline_error_is_preserved(self):
        self.client.request.side_effect = GVError('deadline', 'deadline_exceeded')
        with self.assertRaises(GVError) as error: self.call()
        self.assertEqual(error.exception.code, 'deadline_exceeded')
