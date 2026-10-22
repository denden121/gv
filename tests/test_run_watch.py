import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch

from gitverse_cli.cli import execute, main, parser
from gitverse_cli.client import GVError
from gitverse_cli.run_watch import run_watch

SHA = 'a' * 40


def run(state='success', **kwargs):
    return {'id': 7, 'status': state, 'commit_sha': SHA, **kwargs}


def job(state='success', **kwargs):
    return {'id': 9, 'run_id': 7, 'head_sha': SHA, 'status': state, **kwargs}


def poll(state='success', jobs=None, after=None, **kwargs):
    r = run(state, **kwargs)
    return [r, {'jobs': [job(state)] if jobs is None else jobs, 'total_count': 1 if jobs is None else len(jobs)}, r if after is None else after]


class Clock:
    now = 0
    def monotonic(self): return self.now
    def sleep(self, seconds): self.now += seconds


class WatchTests(unittest.TestCase):
    def watch(self, replies, *flags):
        self.client = Mock(dry_run=False)
        self.client.request.side_effect = replies
        args = parser().parse_args(['run', 'watch', '7', '-R', 'o/r', '--interval', '2', '--timeout', '5', *flags])
        self.clock = Clock()
        with patch('gitverse_cli.run_watch.time.monotonic', self.clock.monotonic), patch('gitverse_cli.run_watch.time.sleep', self.clock.sleep):
            return run_watch(self.client, 'o/r', args)

    def test_pending_to_success_waits_and_reads_only(self):
        result = self.watch(poll('waiting') + poll('running') + poll())
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.payload['polls'], 3)
        self.assertEqual(result.payload['outcome'], 'success')
        self.assertEqual(self.clock.now, 4)
        self.assertTrue(all(c.args[0] == 'GET' for c in self.client.request.call_args_list))
        self.assertTrue(all(c.kwargs['deadline'] == 5 for c in self.client.request.call_args_list))

    def test_exit_status_controls_terminal_failure_and_cancel(self):
        for status in ('failure', 'cancelled'):
            self.assertEqual(self.watch(poll(status)).exit_code, 0)
            self.assertEqual(self.watch(poll(status), '--exit-status').exit_code, 1)

    def test_job_failure_is_not_hidden_by_successful_run(self):
        result = self.watch(poll(jobs=[job('failure')]), '--exit-status')
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.payload['outcome'], 'failure')

    def test_waits_for_pending_jobs_even_if_run_failed(self):
        result = self.watch(poll('failure', jobs=[job('running')]) + poll('failure'), '--exit-status')
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.payload['polls'], 2)

    def test_timeout_is_nonzero_without_exit_status(self):
        result = self.watch(poll('waiting') * 3)
        self.assertEqual(result.exit_code, 8)
        self.assertTrue(result.payload['timed_out'])
        self.assertEqual(self.clock.now, 5)

    def test_unknown_status_no_jobs_and_sha_mismatch_cannot_pass(self):
        for replies in (poll('new-status'), poll(jobs=[job('new-status')]), poll(jobs=[]), poll(jobs=[job(head_sha='b' * 40)])):
            result = self.watch(replies)
            self.assertEqual(result.exit_code, 3)

    def test_skipped_is_reported_separately(self):
        result = self.watch(poll('skipped'), '--exit-status')
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.payload['outcome'], 'skipped')

    def test_need_approval_keeps_waiting(self):
        result = self.watch(poll(need_approval=True) * 3)
        self.assertEqual(result.exit_code, 8)

    def test_run_status_changes_during_snapshot_require_new_poll(self):
        result = self.watch(poll('running', after=run()) + poll())
        self.assertEqual(result.payload['polls'], 2)
        self.assertEqual(result.exit_code, 0)

    def test_rerun_same_id_restarts_snapshot_and_is_followed(self):
        result = self.watch(poll(after=run('running', started_at='new')) + poll('running', started_at='new') + poll(started_at='new'))
        self.assertEqual(result.payload['polls'], 3)
        self.assertEqual(result.exit_code, 0)

    def test_real_run_started_stopped_fields_detect_new_attempt(self):
        result = self.watch(poll(after=run(started=200, stopped=300), started=100, stopped=150) + poll(started=200, stopped=300))
        self.assertEqual(result.payload['polls'], 2)

    def test_changed_commit_during_and_between_polls_stops(self):
        for replies in (poll(after=run(commit_sha='b' * 40)), poll('running') + [run(commit_sha='b' * 40)]):
            result = self.watch(replies)
            self.assertEqual(result.exit_code, 3)
            self.assertEqual(result.payload['reason'], 'run_commit_changed')

    def test_deadline_without_snapshot_and_mid_poll(self):
        for replies in ([GVError('deadline', 'deadline_exceeded')], poll('running') + [GVError('deadline', 'deadline_exceeded')]):
            result = self.watch(replies)
            self.assertEqual(result.exit_code, 8)
            self.assertFalse(result.payload['coverage']['jobs_complete'])

    def test_wrong_run_job_and_malformed_commit_fail(self):
        for replies in ([run(id=8)], [run(commit_sha=None)], poll(jobs=[job(run_id=8)])):
            with self.assertRaises(GVError): self.watch(replies)

    def test_pagination_honors_server_cap(self):
        replies = [run(), {'jobs': [job()], 'total_count': 2}, {'jobs': [job(id=10)], 'total_count': 2}, run()]
        result = self.watch(replies)
        self.assertEqual(len(result.payload['jobs']), 2)
        self.assertTrue(result.payload['coverage']['jobs_complete'])

    def test_repeated_pages_bound_and_empty_before_total_refuse_success(self):
        for replies, flags in (([run(), {'jobs': [job()], 'total_count': 2}, {'jobs': [job()], 'total_count': 2}], []),
                               ([run(), {'jobs': [job()], 'total_count': 2}], ['--max-pages', '1']),
                               ([run(), {'jobs': [], 'total_count': 1}], [])):
            with self.assertRaises(GVError): self.watch(replies, *flags)

    def test_api_failures_are_not_converted_to_timeout(self):
        with self.assertRaises(GVError) as error: self.watch([GVError('forbidden', 'http_error', 403)])
        self.assertEqual(error.exception.status, 403)

    def test_cli_flags_dry_run_and_backend(self):
        for flags in (['--interval', '0'], ['--timeout', 'inf'], ['--max-pages', '-1']):
            with self.assertRaises(GVError): parser().parse_args(['run', 'watch', '7', *flags])
        result = execute(parser().parse_args(['run', 'watch', '7', '-R', 'o/r', '--dry-run', '--exit-status']))
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.payload['exit_status'])
        with self.assertRaises(GVError): execute(parser().parse_args(['run', 'watch', '7', '-R', 'o/r', '--backend', 'web', '--dry-run']))

    def test_cli_fields_keep_nonzero_exit_and_final_json(self):
        out = io.StringIO()
        with patch('gitverse_cli.cli.Client') as factory:
            factory.return_value.backend = 'public'; factory.return_value.dry_run = False
            factory.return_value.request.side_effect = poll('cancelled')
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as error:
                main(['run', 'watch', '7', '-R', 'o/r', '--exit-status', '--fields', 'outcome,exit_code'])
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(json.loads(out.getvalue()), {'outcome': 'cancelled', 'exit_code': 1})
