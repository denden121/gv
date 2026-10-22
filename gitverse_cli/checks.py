"""CI checks for a pinned PR head. Never imply branch-protection completeness."""
from dataclasses import dataclass
import re
import time

from .checkout import git_host
from .client import GVError


@dataclass
class ChecksResult:
    payload: dict
    exit_code: int


JSON_FIELDS = ('bucket', 'completedAt', 'description', 'event', 'link', 'name', 'startedAt', 'state', 'workflow')


def validate_json(args):
    fields = getattr(args, 'json', None)
    if not isinstance(fields, str):
        return
    names = fields.split(',')
    if not names or any(name not in JSON_FIELDS for name in names):
        raise GVError('Select --json fields: ' + ','.join(JSON_FIELDS), 'usage_error')
    if args.watch or getattr(args, 'fields', None):
        raise GVError('--json FIELD,... cannot be combined with --watch or --fields', 'usage_error')


def export_checks(data, fields):
    # Keep native state strings and unknown buckets: never manufacture success.
    return [{key: check.get(key) for key in dict.fromkeys(fields.split(','))}
            for check in data['checks']]


def bucket(status):
    value = status.lower() if isinstance(status, str) else ''
    if value in ('success', 'passed', 'pass'):
        return 'pass'
    if value in ('failure', 'failed', 'error', 'timed_out', 'timeout'):
        return 'fail'
    if value in ('cancelled', 'canceled'):
        return 'cancel'
    if value in ('skipped', 'neutral'):
        return 'skipping'
    if value in ('waiting', 'queued', 'pending', 'running', 'in_progress', 'scheduled', 'not_started', 'blocked', 'waiting_for_approval'):
        return 'pending'
    return 'unknown'


def items_page(data, key):
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict) and isinstance(data.get(key), list):
        total = data.get('total_count')
        if total is not None and (type(total) is not int or total < 0):
            raise GVError('Invalid pagination count in CI response', 'unexpected_response')
        return data[key], total
    raise GVError('Unknown CI pagination envelope', 'unexpected_response')


def read(client, path, deadline, query=None):
    return client.request('GET', path, query, deadline=deadline,
                          require_complete=True, max_response_bytes=1048576)


def all_pages(client, path, key, max_pages, deadline, budget=None):
    collected, seen, expected_total = [], set(), None
    for page in range(1, max_pages + 1):
        data = read(client, path, deadline, {'page': page, 'per_page': 100})
        rows, total = items_page(data, key)
        # Observed jobs DTO: an exhausted page reports zero instead of the
        # catalog total. Accept only after satisfying the preceding total.
        if key == 'jobs' and not rows and total == 0 and expected_total == len(collected):
            return collected, True, None
        if total is not None:
            if expected_total is not None and total != expected_total:
                return collected, False, 'shifting_total_count'
            expected_total = total
        for row in rows:
            if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0:
                raise GVError('CI response contains an invalid run/job ID', 'unexpected_response')
        ids = {r['id'] for r in rows}
        if rows and (ids & seen or len(ids) != len(rows)):
            return collected, False, 'repeated_or_shifting_pages'
        seen.update(ids)
        if budget is not None:
            budget[0] -= len(rows)
            if budget[0] < 0:
                raise GVError('Combined CI run/job entry limit exceeded', 'checks_limit')
        collected.extend(rows)
        if expected_total is not None and len(collected) > expected_total:
            return collected, False, 'total_count_mismatch'
        if not rows:
            if expected_total is not None and len(collected) != expected_total:
                return collected, False, 'empty_page_before_total'
            return collected, True, None
        # Even a short page or matching total is not proof of exhaustion.
        # Read through an empty page; page limits must never imply success.
    return collected, False, 'pagination_limit'


def head_sha(pull):
    head = pull.get('head') if isinstance(pull, dict) else None
    value = head.get('sha') if isinstance(head, dict) else None
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', value):
        raise GVError('PR response has no valid head SHA', 'unexpected_response')
    return value.lower()


def pull_head(client, repo, number, deadline, expected_id=None):
    pull = read(client, f'repos/{repo}/pulls/{number}', deadline)
    if (not isinstance(pull, dict) or type(pull.get('id')) is not int or pull['id'] <= 0
            or type(pull.get('number')) is not int or pull['number'] != number):
        raise GVError('PR identity was not confirmed', 'unexpected_response')
    base = pull.get('base')
    base_repo = base.get('repo') if isinstance(base, dict) else None
    name = base_repo.get('full_name') if isinstance(base_repo, dict) else None
    if not isinstance(name, str) or name.lower() != repo.lower():
        raise GVError('PR base repository identity was not confirmed', 'unexpected_response')
    if expected_id is not None and pull['id'] != expected_id:
        raise GVError('PR identity changed during checks collection', 'pr_changed')
    return pull['id'], head_sha(pull)


def latest_runs(runs, sha):
    latest = {}
    for run in runs:
        commit = run.get('commit_sha')
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', commit):
            raise GVError('A CI run has no commit_sha; cannot safely identify PR checks', 'unexpected_response')
        if commit.lower() != sha:
            continue
        name = run.get('name')
        for field in ('name', 'event', 'ref'):
            if run.get(field) is not None and not isinstance(run[field], str):
                raise GVError('Invalid workflow context in CI response', 'unexpected_response')
        if 'need_approval' in run and type(run['need_approval']) is not bool:
            raise GVError('Invalid workflow approval state in CI response', 'unexpected_response')
        # Preserve separate contexts for push/pull_request/dispatch and refs.
        # Missing workflow identity must not collapse unrelated runs.
        key = (name, run['event'], run['ref']) if all(run.get(k) for k in ('name', 'event', 'ref')) else ('unknown_workflow', run['id'])
        if key not in latest or latest[key]['id'] < run['id']:
            latest[key] = run
    return sorted(latest.values(), key=lambda r: r['id'])


def row(run, job, repo, sha):
    workflow = run.get('name') or f"run-{run['id']}"
    source = job if job is not None else run
    for field in ('name', 'status'):
        if source.get(field) is not None and not isinstance(source[field], str):
            raise GVError('Invalid CI name or state', 'unexpected_response')
    raw_state = source.get('status')
    result = {
        'kind': 'job' if job is not None else 'workflow',
        'name': workflow + (' / ' + str(job.get('name') or job['id']) if job is not None else ''),
        'workflow': workflow, 'event': run.get('event'), 'ref': run.get('ref'),
        'run_id': run['id'], 'head_sha': sha, 'state': raw_state, 'bucket': bucket(raw_state),
        'required': None,
    }
    for native, exported in (('started_at', 'startedAt'), ('completed_at', 'completedAt')):
        value = source.get(native)
        if value is not None and not isinstance(value, str):
            raise GVError('Invalid CI timestamp', 'unexpected_response')
        result[exported] = value
    # Public CI DTOs have no equivalent check description.
    result['description'] = None
    index = run.get('index')
    if type(index) is int and index > 0:
        result['link'] = f'https://{git_host()}/{repo}/cicd/{index}'
    if job is not None:
        result['job_id'] = job['id']
        job_sha = job.get('head_sha')
        if not isinstance(job_sha, str) or job_sha.lower() != sha:
            result['bucket'] = 'unknown'
            result['reason'] = 'job_sha_mismatch'
        if type(job.get('run_id')) is not int or job['run_id'] != run['id']:
            result['bucket'] = 'unknown'
            result['reason'] = 'job_run_mismatch'
    elif run.get('need_approval') and result['bucket'] == 'pass':
        result['bucket'] = 'pending'
        result['reason'] = 'run_needs_approval'
    return result


def snapshot(client, repo, number, sha, args, deadline, pull_id=None):
    budget = [getattr(args, 'max_entries', 100000)]
    runs, complete, issue = all_pages(client, f'repos/{repo}/actions/runs', 'workflow_runs', args.max_pages, deadline, budget)
    issues = [issue] if issue else []
    checks = []
    selected = latest_runs(runs, sha)
    for run in selected:
        jobs, jobs_complete, jobs_issue = all_pages(client, f"repos/{repo}/actions/runs/{run['id']}/jobs", 'jobs', args.max_pages, deadline, budget)
        complete = complete and jobs_complete
        if jobs_issue:
            issues.append(f"run {run['id']}: {jobs_issue}")
        workflow = row(run, None, repo, sha)
        if not jobs and workflow['bucket'] == 'pass':
            workflow['bucket'] = 'unknown'
            workflow['reason'] = 'successful_workflow_has_no_visible_jobs'
        checks.append(workflow)
        checks.extend(row(run, job, repo, sha) for job in jobs)
    counts = {key: sum(c['bucket'] == key for c in checks) for key in ('pass', 'fail', 'cancel', 'pending', 'skipping', 'unknown')}
    if counts['fail'] or counts['cancel']:
        ci_state = 'fail'
    elif counts['pending']:
        ci_state = 'pending'
    elif not checks:
        ci_state = 'no_checks'
    elif counts['unknown'] or not complete:
        ci_state = 'unknown'
    elif counts['pass']:
        ci_state = 'pass'
    else:
        ci_state = 'skipping'
    # Check again after collecting data: never report the old commit as current.
    _, current = pull_head(client, repo, number, deadline, pull_id)
    changed = current != sha
    scope_complete = bool(args.ci_only and complete and not counts['unknown'] and not changed)
    return {
        'repository': repo, 'number': number, 'head_sha': sha,
        'current_head_sha': current, 'head_changed': changed,
        'scope': 'ci' if args.ci_only else 'all_checks',
        'coverage': {'ci_scan_complete': complete, 'external_statuses_known': False, 'required_checks_known': False, 'scope_complete': scope_complete},
        'ci_state': ci_state, 'summary': counts, 'checks': checks,
        'runs_scanned': len(runs), 'runs_for_head': len(selected),
        'scan_issues': issues,
        'note': 'CI from the base repository only; collection is not atomic. External commit statuses and branch-protection requirements have no verified API contract here and remain unresolved. A CI pass is not permission to merge.',
    }


def result_code(data, ci_only):
    if data.get('head_changed'):
        return 3
    counts = data.get('summary', {})
    if counts.get('fail') or counts.get('cancel'):
        return 1
    if counts.get('pending'):
        return 8
    if not data.get('coverage', {}).get('ci_scan_complete') or counts.get('unknown'):
        return 3
    if not data.get('checks'):
        return 4
    if not ci_only:
        return 3
    return 0


def pr_checks(args, client, repo, number):
    from .cli import require_public
    validate_json(args)
    require_public(client, 'pr checks')
    if args.required:
        raise GVError('The verified public API does not expose required checks; --required cannot be evaluated', 'required_checks_unavailable')
    if client.dry_run:
        return ChecksResult({'pull_request': client.request('GET', f'repos/{repo}/pulls/{number}'),
                             'runs': client.request('GET', f'repos/{repo}/actions/runs', {'page': 1, 'per_page': 100}),
                             'plan': 'Paginate runs, select exact head.sha, keep latest workflow/event/ref attempts, fetch all jobs, recheck PR head. No branch filter.',
                             'ci_only': args.ci_only, 'watch': args.watch, 'timeout': args.timeout, 'max_pages': args.max_pages,
                             'max_entries': getattr(args, 'max_entries', 100000), 'max_response_bytes': 1048576,
                             'json_fields': args.json.split(',') if isinstance(getattr(args, 'json', None), str) else None}, 0)
    start = time.monotonic()
    deadline = start + args.timeout
    sha, last = None, None
    polls = 0
    try:
        pull_id, sha = pull_head(client, repo, number, deadline)
        while True:
            if time.monotonic() >= deadline:
                break
            _, current = pull_head(client, repo, number, deadline, pull_id)
            if current != sha:
                data = last or {'repository': repo, 'number': number, 'head_sha': sha, 'checks': []}
                data.update({'current_head_sha': current, 'head_changed': True, 'reason': 'pr_head_changed', 'polls': polls, 'exit_code': 3})
                data.setdefault('coverage', {})['scope_complete'] = False
                return ChecksResult(data, 3)
            last = snapshot(client, repo, number, sha, args, deadline, pull_id)
            polls += 1
            last['polls'] = polls
            if last['head_changed']:
                last['reason'] = 'pr_head_changed'
            code = result_code(last, args.ci_only)
            pending = last['summary']['pending'] > 0
            waiting_for_appearance = not last['checks'] and last['coverage']['ci_scan_complete']
            if (not args.watch or last['head_changed'] or
                    (args.fail_fast and code == 1) or
                    (not pending and not waiting_for_appearance) or
                    not last['coverage']['ci_scan_complete']):
                last['exit_code'] = code
                return ChecksResult(last, code)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(args.interval, remaining))
    except GVError as e:
        if e.code != 'deadline_exceeded':
            raise
    data = last or {'repository': repo, 'number': number, 'head_sha': sha, 'checks': [], 'ci_state': 'unknown', 'coverage': {'scope_complete': False, 'ci_scan_complete': False}}
    data['coverage']['scope_complete'] = False
    data.update({'timed_out': True, 'reason': 'timeout', 'polls': polls, 'exit_code': 8})
    return ChecksResult(data, 8)
