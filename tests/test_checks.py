import contextlib
import copy
import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from gitverse_cli.checks import all_pages, bucket, latest_runs, pr_checks
from gitverse_cli.cli import execute, main, parser
from gitverse_cli.client import Client, GVError


SHA = 'a' * 40
OTHER = 'b' * 40
REPO = 'owner/repo'


def run(identifier=1, state='success', **extra):
    return {'id': identifier, 'index': identifier, 'name': 'test.yml', 'status': state, 'commit_sha': SHA, 'event': 'pull_request', 'ref': 'refs/pull/1/head', **extra}


def job(identifier=10, state='success', run_id=1, **extra):
    return {'id': identifier, 'name': 'verify', 'status': state, 'run_id': run_id, 'head_sha': SHA, **extra}


def cycle(run_state='success', job_state='success'):
    return {'runs': [run(state=run_state)], 'jobs': {1: [job(state=job_state)]}}


class Clock:
    def __init__(self):
        self.now = 0
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class FakeClient:
    backend = 'public'
    dry_run = False
    def __init__(self, cycles=None):
        self.cycles = cycles or [cycle()]
        self.index = -1
        self.calls = []
        self.current = self.cycles[0]
    def request(self, method, path, query=None, deadline=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(query)))
        if path == 'repos/owner/repo/pulls/1':
            value = self.current.get('head_after', SHA) if self.index >= 0 else SHA
            return {'id': 101, 'number': 1, 'base': {'repo': {'full_name': REPO}}, 'head': {'sha': value}}
        if path == 'repos/owner/repo/actions/runs':
            page = query['page']
            if page == 1:
                self.index += 1
                self.current = self.cycles[min(self.index, len(self.cycles) - 1)]
            pages = self.current.get('runs_pages', {1: self.current['runs']})
            rows = pages.get(page, [])
            return {'workflow_runs': copy.deepcopy(rows), 'total_count': self.current.get('runs_total', sum(len(p) for p in pages.values()))}
        if path.endswith('/jobs'):
            identifier = int(path.split('/')[-2])
            rows = self.current.get('jobs', {}).get(identifier, [])
            pages = rows if isinstance(rows, dict) else {1: rows}
            return {'jobs': copy.deepcopy(pages.get(query['page'], [])), 'total_count': self.current.get('jobs_total', sum(len(p) for p in pages.values()))}
        raise AssertionError(path)


class ChecksTests(unittest.TestCase):
    def check(self, client=None, *flags):
        args = parser().parse_args(['pr', 'checks', '1', '-R', REPO, *flags])
        clock = Clock()
        with patch('gitverse_cli.checks.time.monotonic', clock.monotonic), patch('gitverse_cli.checks.time.sleep', clock.sleep):
            return pr_checks(args, client or FakeClient(), REPO, 1)

    def test_pass_requires_explicit_ci_scope(self):
        result = self.check(None, '--ci-only')
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.payload['ci_state'], 'pass')
        self.assertFalse(result.payload['coverage']['required_checks_known'])
        self.assertTrue(result.payload['coverage']['scope_complete'])
        result = self.check()
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(result.payload['coverage']['scope_complete'])

    def test_failed_cancelled_pending_unknown_and_all_skipped(self):
        for state, code in [('failure', 1), ('cancelled', 1), ('waiting', 8), ('future_status', 3), ('skipped', 0)]:
            with self.subTest(state=state):
                result = self.check(FakeClient([cycle(state, state)]), '--ci-only')
                self.assertEqual(result.exit_code, code)

    def test_exact_sha_only_and_no_branch_filter(self):
        c = cycle()
        c['runs'].append(run(2, 'failure', commit_sha=OTHER))
        client = FakeClient([c])
        result = self.check(client, '--ci-only')
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.payload['runs_for_head'], 1)
        self.assertTrue(all(call[0] == 'GET' for call in client.calls))
        self.assertTrue(all(not q or 'branch' not in q for _, _, q in client.calls))

    def test_latest_attempt_does_not_hide_another_workflow_or_event(self):
        runs = [run(1, 'failure'), run(2, 'success'), run(3, 'failure', event='push'), run(4, 'failure', name='other.yml')]
        self.assertEqual([r['id'] for r in latest_runs(runs, SHA)], [2, 3, 4])

    def test_unknown_workflow_names_do_not_collapse_checks(self):
        rows = [run(1, name=None), run(2, name=None)]
        self.assertEqual(len(latest_runs(rows, SHA)), 2)

    def test_run_failure_is_not_hidden_by_passing_jobs(self):
        result = self.check(FakeClient([cycle('failure', 'success')]), '--ci-only')
        self.assertEqual(result.exit_code, 1)

    def test_pending_workflow_is_not_hidden_by_completed_jobs(self):
        result = self.check(FakeClient([cycle('running', 'success')]), '--ci-only')
        self.assertEqual(result.exit_code, 8)

    def test_job_sha_or_run_mismatch_prevents_success(self):
        for changed in ({'head_sha': OTHER}, {'run_id': 999}):
            c = cycle()
            c['jobs'][1][0].update(changed)
            self.assertEqual(self.check(FakeClient([c]), '--ci-only').exit_code, 3)

    def test_success_without_visible_jobs_is_unknown(self):
        c = cycle(); c['jobs'] = {}
        result = self.check(FakeClient([c]), '--ci-only')
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.payload['checks'][0]['reason'], 'successful_workflow_has_no_visible_jobs')

    def test_approval_state_is_boolean_and_cannot_silently_pass(self):
        for value in (None, 0, '', [], 'false'):
            c = cycle()
            c['runs'][0]['need_approval'] = value
            with self.subTest(value=value), self.assertRaises(GVError) as error:
                self.check(FakeClient([c]), '--ci-only')
            self.assertEqual(error.exception.code, 'unexpected_response')
        c = cycle()
        c['runs'][0]['need_approval'] = True
        self.assertEqual(self.check(FakeClient([c]), '--ci-only').exit_code, 8)

    def test_no_runs_is_not_success(self):
        result = self.check(FakeClient([{'runs': [], 'jobs': {}}]), '--ci-only')
        self.assertEqual(result.exit_code, 4)
        self.assertEqual(result.payload['ci_state'], 'no_checks')

    def test_watch_pending_to_pass_and_wait_for_first_run(self):
        for cycles in ([cycle('running', 'running'), cycle()], [{'runs': [], 'jobs': {}}, cycle()]):
            c = FakeClient(cycles)
            result = self.check(c, '--ci-only', '--watch', '--interval', '1', '--timeout', '5')
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.payload['polls'], 2)

    def test_watch_timeout_is_nonzero_and_bounded(self):
        c = FakeClient([cycle('running', 'waiting')])
        result = self.check(c, '--ci-only', '--watch', '--interval', '2', '--timeout', '3')
        self.assertEqual(result.exit_code, 8)
        self.assertTrue(result.payload['timed_out'])
        self.assertEqual(result.payload['polls'], 2)

    def test_watch_fail_fast_stops_with_other_jobs_still_pending(self):
        c = cycle('running', 'failure')
        result = self.check(FakeClient([c, cycle()]), '--ci-only', '--watch', '--fail-fast')
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.payload['polls'], 1)

    def test_watch_without_fail_fast_waits_for_all_jobs(self):
        first = cycle('running', 'failure')
        last = cycle('failure', 'failure')
        result = self.check(FakeClient([first, last]), '--ci-only', '--watch', '--interval', '1')
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.payload['polls'], 2)

    def test_head_change_during_fetch_is_not_reported_as_pass(self):
        c = cycle(); c['head_after'] = OTHER
        result = self.check(FakeClient([c]), '--ci-only')
        self.assertEqual(result.exit_code, 3)
        self.assertTrue(result.payload['head_changed'])
        self.assertFalse(result.payload['coverage']['scope_complete'])
        self.assertEqual(result.payload['head_sha'], SHA)
        self.assertEqual(result.payload['reason'], 'pr_head_changed')

    def test_max_pages_prevents_false_green_even_if_visible_jobs_pass(self):
        c = cycle(); c['runs_total'] = 2
        result = self.check(FakeClient([c]), '--ci-only', '--max-pages', '1')
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(result.payload['coverage']['ci_scan_complete'])

    def test_head_change_between_polls_invalidates_previous_coverage(self):
        class MovingHead(FakeClient):
            pulls = 0
            def request(self, method, path, query=None, deadline=None, **kwargs):
                if path.endswith('/pulls/1'):
                    self.pulls += 1
                    if self.pulls >= 4:
                        return {'id': 101, 'number': 1, 'base': {'repo': {'full_name': REPO}}, 'head': {'sha': OTHER}}
                return super().request(method, path, query, deadline)
        result = self.check(MovingHead([cycle('running', 'running')]), '--ci-only', '--watch', '--interval', '1')
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.payload['polls'], 1)
        self.assertFalse(result.payload['coverage']['scope_complete'])

    def test_capped_page_size_uses_total_count(self):
        c = cycle()
        c['runs_pages'] = {1: [run()], 2: [run(2, name='second.yml')]}
        c['jobs'][2] = [job(20, run_id=2)]
        result = self.check(FakeClient([c]), '--ci-only')
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.payload['runs_scanned'], 2)

    def test_repeated_and_empty_pages_are_incomplete(self):
        for pages in ({1: [run()], 2: [run()]}, {1: [run()], 2: []}):
            c = cycle(); c['runs_pages'] = pages; c['runs_total'] = 2
            result = self.check(FakeClient([c]), '--ci-only')
            self.assertEqual(result.exit_code, 3)

    def test_job_pagination_bound_also_prevents_success(self):
        c = cycle(); c['jobs_total'] = 2
        result = self.check(FakeClient([c]), '--ci-only', '--max-pages', '1')
        self.assertEqual(result.exit_code, 3)

    def test_unknown_api_shape_or_missing_run_sha_fails(self):
        c = cycle(); c['runs'][0].pop('commit_sha')
        with self.assertRaises(GVError):
            self.check(FakeClient([c]), '--ci-only')
        client = Mock(); client.request.return_value = {'unexpected': []}
        with self.assertRaises(GVError):
            all_pages(client, 'path', 'workflow_runs', 2, 100)

    def test_required_is_explicitly_unsupported(self):
        c = FakeClient()
        with self.assertRaises(GVError) as e:
            self.check(c, '--required')
        self.assertEqual(e.exception.code, 'required_checks_unavailable')
        self.assertFalse(c.calls)

    def test_dry_run_without_credentials_or_network(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError):
            args = parser().parse_args(['pr', 'checks', '1', '-R', REPO, '--dry-run', '--watch'])
            result = pr_checks(args, Client(dry_run=True), REPO, 1)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.payload['pull_request']['method'], 'GET')

    def test_cli_url_and_json_fields_preserve_nonzero_exit(self):
        c = FakeClient([cycle('running', 'waiting')])
        out = io.StringIO()
        with patch('gitverse_cli.cli.Client', return_value=c), contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as e:
            main(['pr', 'checks', 'https://gitverse.ru/owner/repo/pulls/1', '--ci-only', '--fields', 'ci_state,exit_code'])
        self.assertEqual(e.exception.code, 8)
        self.assertEqual(json.loads(out.getvalue()), {'ci_state': 'pending', 'exit_code': 8})

    def test_invalid_intervals_and_timeouts_are_rejected(self):
        for option, value in [('--interval', '0'), ('--timeout', '-1'), ('--interval', 'nan'), ('--timeout', 'inf')]:
            with self.assertRaises(GVError):
                parser().parse_args(['pr', 'checks', '1', option, value])

    def test_unknown_status_is_never_pass(self):
        self.assertEqual(bucket('new_server_status'), 'unknown')
        self.assertEqual(bucket(None), 'unknown')

    def test_short_pages_without_totals_are_read_to_empty(self):
        client = Mock()
        client.request.side_effect = [[run()], [run(2)], []]
        rows, complete, issue = all_pages(client, 'path', 'workflow_runs', 3, 100)
        self.assertEqual([r['id'] for r in rows], [1, 2])
        self.assertTrue(complete)
        self.assertIsNone(issue)
        for call in client.request.call_args_list:
            self.assertTrue(call.kwargs['require_complete'])
            self.assertEqual(call.kwargs['max_response_bytes'], 1048576)
            self.assertEqual(call.kwargs['deadline'], 100)

    def test_count_exhaustion_still_requires_empty_page(self):
        client = Mock()
        client.request.return_value = {'workflow_runs': [run()], 'total_count': 1}
        _, complete, issue = all_pages(client, 'path', 'workflow_runs', 1, 100)
        self.assertFalse(complete)
        self.assertEqual(issue, 'pagination_limit')

    def test_observed_empty_jobs_page_zero_total(self):
        client = Mock()
        client.request.side_effect = [{'jobs': [job()], 'total_count': 1},
                                      {'jobs': [], 'total_count': 0}]
        self.assertTrue(all_pages(client, 'path', 'jobs', 2, 100)[1])
        client.request.side_effect = [{'jobs': [job()], 'total_count': 2},
                                      {'jobs': [], 'total_count': 0}]
        self.assertFalse(all_pages(client, 'path', 'jobs', 2, 100)[1])

    def test_shifting_or_underreported_totals_never_complete(self):
        for pages in ([{'workflow_runs': [run()], 'total_count': 0}],
                      [{'workflow_runs': [run()], 'total_count': 1},
                       {'workflow_runs': [], 'total_count': 0}]):
            client = Mock(); client.request.side_effect = pages
            self.assertFalse(all_pages(client, 'path', 'workflow_runs', 3, 100)[1])

    def test_combined_entries_limit_includes_jobs(self):
        with self.assertRaises(GVError) as error:
            self.check(None, '--ci-only', '--max-entries', '1')
        self.assertEqual(error.exception.code, 'checks_limit')

    def test_malformed_pull_identity_and_wrong_scope_refuse_before_ci(self):
        for change in ({'id': True}, {'number': 2}, {'base': []},
                       {'base': {'repo': {'full_name': 'other/repo'}}}, {'head': 'invalid'}):
            client = FakeClient()
            original = client.request
            def request(method, path, query=None, **kwargs):
                result = original(method, path, query, **kwargs)
                if path.endswith('/pulls/1'): result.update(change)
                return result
            client.request = request
            with self.subTest(change=change), self.assertRaises(GVError):
                self.check(client, '--ci-only')
            self.assertFalse(any('/actions/' in call[1] for call in client.calls))

    def test_pull_id_change_on_readback_refuses(self):
        client = FakeClient(); original = client.request
        def request(method, path, query=None, **kwargs):
            result = original(method, path, query, **kwargs)
            if path.endswith('/pulls/1') and client.index >= 0: result['id'] = 102
            return result
        client.request = request
        with self.assertRaises(GVError) as error:
            self.check(client, '--ci-only')
        self.assertEqual(error.exception.code, 'pr_changed')

    def test_permission_denial_is_not_no_checks(self):
        for status in (401, 403, 404):
            client = Mock(backend='public', dry_run=False)
            client.request.side_effect = GVError('Access denied', 'http_error', status)
            with self.subTest(status=status), self.assertRaises(GVError) as error:
                self.check(client, '--ci-only')
            self.assertEqual(error.exception.status, status)
            self.assertEqual(client.request.call_count, 1)

    def test_missing_job_scope_is_unknown(self):
        for field in ('run_id', 'head_sha'):
            c = cycle(); c['jobs'][1][0].pop(field)
            self.assertEqual(self.check(FakeClient([c]), '--ci-only').exit_code, 3)

    def test_missing_context_does_not_hide_previous_failure(self):
        for field in ('event', 'ref'):
            rows = [run(1, 'failure'), run(2)]
            for value in rows: value.pop(field)
            self.assertEqual(len(latest_runs(rows, SHA)), 2)
        with self.assertRaises(GVError):
            latest_runs([run(event=[])], SHA)

    def test_all_pull_reads_are_bounded(self):
        client = FakeClient(); original = client.request
        def request(method, path, query=None, **kwargs):
            self.assertTrue(kwargs['require_complete'])
            self.assertEqual(kwargs['max_response_bytes'], 1048576)
            return original(method, path, query, **kwargs)
        client.request = request
        self.assertEqual(self.check(client, '--ci-only').exit_code, 0)

    def test_web_backend_refuses_before_any_request(self):
        client = FakeClient()
        client.backend = 'web'
        with self.assertRaises(GVError):
            self.check(client, '--ci-only')
        self.assertEqual(client.calls, [])

    def test_invalid_catalog_rows_and_counts_refuse(self):
        for key in ('workflow_runs', 'jobs'):
            for response in ({key: [None]}, {key: [{'id': True}]},
                             {key: [{'id': 0}]}, {key: [], 'total_count': True},
                             {key: [], 'total_count': -1}):
                client = Mock()
                client.request.return_value = response
                with self.subTest(response=response), self.assertRaises(GVError):
                    all_pages(client, 'path', key, 2, 100)

    def test_transport_limits_apply_to_pr_runs_and_jobs(self):
        class Response(io.BytesIO):
            status = 200
        pull = {'id': 101, 'number': 1, 'base': {'repo': {'full_name': REPO}},
                'head': {'sha': SHA}}
        # Two PR reads precede the runs scan; jobs follow the exhausted runs list.
        preceding = [pull, pull, {'workflow_runs': [run()], 'total_count': 1},
                     {'workflow_runs': [], 'total_count': 1}]
        for offset in (0, 2, 4):
            for status, body, expected in ((206, b'{}', 'incomplete_response'),
                                           (200, b'x' * 1048577, 'response_limit'),
                                           (200, b'\xff', 'unexpected_response')):
                client = Client(dry_run=True)
                client.dry_run = False
                responses = [Response(json.dumps(d).encode()) for d in preceding[:offset]]
                invalid = Response(body)
                invalid.status = status
                client.opener = Mock()
                client.opener.open.side_effect = [*responses, invalid]
                args = parser().parse_args(['pr', 'checks', '1', '-R', REPO, '--ci-only'])
                with self.subTest(offset=offset, status=status, expected=expected), self.assertRaises(GVError) as error:
                    pr_checks(args, client, REPO, 1)
                self.assertEqual(error.exception.code, expected)
                self.assertEqual(client.opener.open.call_count, offset + 1)

    def test_redirect_does_not_forward_public_credentials_or_retry(self):
        client = Client(dry_run=True)
        client.dry_run = False
        client.headers['Authorization'] = 'Bearer synthetic-test-token'
        client.opener = Mock()
        client.opener.open.side_effect = urllib.error.HTTPError(
            client.base, 302, 'refused', {'Location': 'https://other.invalid/checks'}, None)
        args = parser().parse_args(['pr', 'checks', '1', '-R', REPO, '--ci-only'])
        with self.assertRaises(GVError) as error:
            pr_checks(args, client, REPO, 1)
        self.assertEqual(error.exception.status, 302)
        self.assertEqual(client.opener.open.call_count, 1)
        self.assertEqual(client.opener.open.call_args.args[0].full_url,
                         client.base + '/repos/owner/repo/pulls/1')
        from gitverse_cli.client import NoRedirect
        self.assertTrue(any(isinstance(h, NoRedirect) for h in Client(dry_run=True).opener.handlers))
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {},
                                                      'https://other.invalid/checks'))


class DeadlineTests(unittest.TestCase):
    def test_transport_never_opens_a_request_after_deadline(self):
        client = Client(dry_run=True); client.dry_run = False; client.opener = Mock()
        with patch('gitverse_cli.client.time.monotonic', return_value=5), self.assertRaises(GVError) as e:
            client.request('GET', 'user', deadline=4)
        self.assertEqual(e.exception.code, 'deadline_exceeded')
        client.opener.open.assert_not_called()

    def test_retry_after_cannot_escape_deadline(self):
        clock = Clock()
        client = Client(dry_run=True); client.dry_run = False; client.opener = Mock()
        def unavailable(*a, **kw):
            clock.now += 0.8
            raise urllib.error.HTTPError('https://api.gitverse.ru/user', 503, '', {'Retry-After': '60'}, io.BytesIO())
        client.opener.open.side_effect = unavailable
        with patch('gitverse_cli.client.time.monotonic', clock.monotonic), patch('gitverse_cli.client.time.sleep', clock.sleep), self.assertRaises(GVError) as e:
            client.request('GET', 'user', deadline=1)
        self.assertEqual(e.exception.code, 'deadline_exceeded')
        self.assertEqual(client.opener.open.call_count, 1)
        self.assertEqual(clock.now, 1)


if __name__ == '__main__':
    unittest.main()
