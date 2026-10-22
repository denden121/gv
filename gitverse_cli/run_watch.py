"""Bounded CI waiting for an explicit run ID, with JSON output for agents."""
import re
import time

from .checks import ChecksResult, bucket
from .client import GVError
from .run_logs import identity, jobs_for_run


def commit(run):
    sha = run.get('commit_sha')
    if not isinstance(sha, str) or not re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', sha):
        raise GVError('CI run has no valid commit_sha', 'unexpected_response')
    return sha.lower()


def outcome(run, jobs):
    state = bucket(run.get('status'))
    states = [bucket(j.get('status')) for j in jobs]
    if state == 'unknown' or 'unknown' in states:
        return 'unknown'
    if run.get('need_approval') or state == 'pending' or 'pending' in states:
        return 'pending'
    if state == 'fail' or 'fail' in states:
        return 'failure'
    if state == 'cancel' or 'cancel' in states:
        return 'cancelled'
    if state == 'skipping':
        return 'skipped'
    if not jobs:
        return 'unknown'
    return 'success' if 'pass' in states else 'skipped'


def run_watch(client, repo, args):
    path = f'repos/{repo}/actions/runs/{args.run_id}'
    if client.dry_run:
        return ChecksResult({'run_request': client.request('GET', path),
                             'jobs_request': client.request('GET', path + '/jobs', {'page': 1, 'per_page': 100}),
                             'interval': args.interval, 'timeout': args.timeout, 'max_pages': args.max_pages,
                             'exit_status': args.exit_status,
                             'plan': 'Poll explicit run ID and all job pages, reread run, wait until known terminal statuses. Follow reruns of this ID; pin commit_sha. One final JSON; no network in dry-run.'}, 0)
    deadline = time.monotonic() + args.timeout
    last, sha, polls = None, None, 0
    try:
        while time.monotonic() < deadline:
            run = identity(client.request('GET', path, deadline=deadline), args.run_id, finished=False)
            current = commit(run)
            if sha is None: sha = current
            if current != sha:
                return ChecksResult({'repository': repo, 'run_id': args.run_id, 'commit_sha': sha,
                                     'current_commit_sha': current, 'outcome': 'unknown', 'reason': 'run_commit_changed',
                                     'polls': polls, 'coverage': {'jobs_complete': False}, 'exit_code': 3}, 3)
            jobs = jobs_for_run(client, path + '/jobs', args.run_id, args.max_pages, deadline, finished=False)
            after = identity(client.request('GET', path, deadline=deadline), args.run_id, finished=False)
            if commit(after) != sha:
                return ChecksResult({'repository': repo, 'run_id': args.run_id, 'commit_sha': sha,
                                     'current_commit_sha': commit(after), 'outcome': 'unknown', 'reason': 'run_commit_changed',
                                     'polls': polls, 'coverage': {'jobs_complete': False}, 'exit_code': 3}, 3)
            polls += 1
            # A rerun can reuse run/job IDs. A transition during collection
            # requires a fresh poll, never a terminal result from mixed data.
            stable = all(run.get(k) == after.get(k) for k in ('status', 'started', 'stopped', 'started_at', 'completed_at', 'need_approval'))
            mismatch = any(j.get('head_sha') is not None and
                           (not isinstance(j['head_sha'], str) or j['head_sha'].lower() != sha) for j in jobs)
            result = outcome(after, jobs) if stable else 'pending'
            if mismatch: result = 'unknown'
            last = {'repository': repo, 'run_id': args.run_id, 'commit_sha': sha, 'status': after.get('status'),
                    'outcome': result, 'polls': polls, 'timed_out': False,
                    'coverage': {'jobs_complete': True, 'snapshot_stable': stable, 'job_sha_matches': not mismatch},
                    'jobs': [{'id': j['id'], 'name': j.get('name'), 'status': j.get('status'), 'bucket': bucket(j.get('status'))} for j in jobs]}
            if result != 'pending':
                code = 3 if result == 'unknown' else (1 if args.exit_status and result in ('failure', 'cancelled') else 0)
                last['exit_code'] = code
                return ChecksResult(last, code)
            remaining = deadline - time.monotonic()
            if remaining <= 0: break
            time.sleep(min(args.interval, remaining))
    except GVError as error:
        if error.code != 'deadline_exceeded': raise
        # A previous completed snapshot remains useful but is not current.
        if last: last['coverage']['jobs_complete'] = False
    data = last or {'repository': repo, 'run_id': args.run_id, 'commit_sha': sha, 'jobs': [],
                    'outcome': 'unknown', 'coverage': {'jobs_complete': False}}
    data.update(timed_out=True, reason='timeout', polls=polls, exit_code=8)
    return ChecksResult(data, 8)
