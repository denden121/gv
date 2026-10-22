"""Read completed CI logs with exact step ranges and no partial-success output."""
import time

from .checks import bucket, items_page
from .client import GVError


def identity(value, expected, run_id=None, finished=True):
    if not isinstance(value, dict) or type(value.get('id')) is not int or value['id'] != expected:
        raise GVError('CI identity was not confirmed', 'unexpected_response')
    if run_id is not None and value.get('run_id') != run_id:
        raise GVError('Job does not belong to the requested run', 'job_run_mismatch')
    state = bucket(value.get('status'))
    if finished and state in ('pending', 'unknown'):
        raise GVError('Logs require a finished run and jobs with known status', 'logs_not_ready', details={'id': expected, 'status': value.get('status')})
    return value


def jobs_for_run(client, path, run_id, max_pages, deadline, finished=True):
    rows, seen, expected_total = [], set(), None
    for page in range(1, max_pages + 1):
        items, total = items_page(client.request('GET', path, {'page': page, 'per_page': 100}, deadline=deadline), 'jobs')
        if total is not None:
            if expected_total is not None and total != expected_total:
                raise GVError('CI job count changed during pagination', 'pagination_error')
            expected_total = total
        for item in items:
            job_id = item.get('id') if isinstance(item, dict) else None
            if type(job_id) is not int or job_id <= 0 or job_id in seen:
                raise GVError('Invalid or repeated CI job page', 'pagination_error')
            identity(item, job_id, run_id, finished=finished)
            seen.add(job_id); rows.append(item)
        if expected_total is not None and len(rows) > expected_total:
            raise GVError('CI job count contradicts pagination', 'pagination_error')
        if expected_total is not None and len(rows) == expected_total:
            return rows
        if not items:
            if expected_total is not None:
                raise GVError('CI job pages ended before total_count', 'pagination_error')
            return rows
    raise GVError('CI job pagination bound reached; no partial logs returned', 'pagination_limit')


def step_ranges(job):
    steps = job.get('steps')
    if not isinstance(steps, list):
        raise GVError('Missing step metadata for log indexing', 'unexpected_response')
    result, end = [], 0
    for step in steps:
        if not isinstance(step, dict) or bucket(step.get('status')) in ('unknown', 'pending'):
            raise GVError('Unknown CI step status', 'unexpected_response')
        start, length = step.get('log_index'), step.get('log_length')
        if type(start) is not int or type(length) is not int or start < 0 or length < 0 or start != end:
            raise GVError('Step log ranges are missing, overlapping or incomplete', 'log_range_error')
        result.append((step, start, length)); end = start + length
    return result, end


def stable_before_after(before, after):
    # Reruns can reuse IDs. Refuse a changed attempt or changed log metadata.
    keys = ('id', 'run_id', 'status', 'commit_sha', 'head_sha', 'started', 'stopped', 'started_at', 'completed_at', 'steps')
    return all(before.get(k) == after.get(k) for k in keys)


def run_logs(client, repo, args):
    if not args.log and not args.log_failed:
        raise GVError('--job requires --log or --log-failed', 'usage_error')
    path = f'repos/{repo}/actions/runs/{args.run_id}'
    if client.dry_run:
        return {'run_request': client.request('GET', path),
                'jobs_request': client.request('GET', f'repos/{repo}/actions/jobs/{args.job}' if args.job else path + '/jobs'),
                'mode': 'failed_steps' if args.log_failed else 'all',
                'job_log_path_template': f'repos/{repo}/actions/jobs/JOB_ID/logs',
                'plan': 'Read finished run and jobs, validate membership and complete pagination, fetch text logs, verify exact step ranges and unchanged attempt; JSON only. No network in dry-run.'}
    deadline = time.monotonic() + args.timeout
    run = identity(client.request('GET', path, deadline=deadline), args.run_id)
    if args.job:
        jobs = [identity(client.request('GET', f'repos/{repo}/actions/jobs/{args.job}', deadline=deadline), args.job, args.run_id)]
    else:
        jobs = jobs_for_run(client, path + '/jobs', args.run_id, args.max_pages, deadline)
    output = []
    for job in jobs:
        ranges, expected_lines = step_ranges(job)
        failed = [entry for entry in ranges if bucket(entry[0]['status']) == 'fail']
        if args.log_failed and not failed:
            if bucket(job['status']) == 'fail':
                raise GVError('Failed job has no identifiable failed step', 'failed_steps_unavailable', details={'job_id': job['id']})
            continue
        job_path = f"repos/{repo}/actions/jobs/{job['id']}"
        try:
            text = client.request('GET', job_path + '/logs', raw=True, deadline=deadline)
        except GVError as error:
            if error.status == 404:
                raise GVError('Job logs are unavailable or expired; no partial logs returned', 'logs_unavailable', 404, {'run_id': args.run_id, 'job_id': job['id']}) from error
            raise
        if text is None: text = ''
        if not isinstance(text, str) or text.lstrip().lower().startswith(('<!doctype html', '<html')):
            raise GVError('Expected text logs, received another format', 'unexpected_response')
        # GitVerse indexes LF-delimited records. Bare CR is progress output,
        # not a new record (str.splitlines would shift failed-step offsets).
        pieces = text.split('\n')
        lines = [piece + '\n' for piece in pieces[:-1]]
        if pieces[-1]: lines.append(pieces[-1])
        if len(lines) != expected_lines:
            raise GVError('Log line count does not match complete step metadata', 'log_range_error', details={'job_id': job['id'], 'expected_lines': expected_lines, 'actual_lines': len(lines)})
        after = identity(client.request('GET', job_path, deadline=deadline), job['id'], args.run_id)
        if not stable_before_after(job, after):
            raise GVError('Job changed while reading logs; rerun the read', 'ci_attempt_changed')
        result = {'id': job['id'], 'name': job.get('name'), 'status': job['status'], 'line_count': len(lines)}
        if args.log_failed:
            result['steps'] = [{'name': s.get('name'), 'number': s.get('number'), 'status': s['status'],
                                'log_index': start, 'log_length': size, 'log': ''.join(lines[start:start + size])}
                               for s, start, size in failed]
        else:
            result['log'] = text
        output.append(result)
    after = identity(client.request('GET', path, deadline=deadline), args.run_id)
    if not stable_before_after(run, after):
        raise GVError('Run changed while reading logs; rerun the read', 'ci_attempt_changed')
    return {'run_id': args.run_id, 'status': run['status'], 'mode': 'failed_steps' if args.log_failed else 'all',
            'jobs': output, 'coverage': {'complete': True, 'scope': 'selected_job' if args.job else 'run_jobs',
                                        'jobs_scanned': len(jobs)},
            'empty_reason': 'no_failed_steps' if args.log_failed and not output else ('no_jobs' if not jobs else None)}
