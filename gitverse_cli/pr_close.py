"""Verified public PR close with recoverable, non-atomic closing comment."""
import time

from .client import GVError
from .comment_edit import comment_snapshot
from .directory import user_value
from .pr_context import revision


def snapshot(row, repo, number):
    result = revision(row, number)
    base_repo = row['base'].get('repo')
    if (not isinstance(base_repo, dict) or type(base_repo.get('id')) is not int
            or base_repo['id'] <= 0 or not isinstance(base_repo.get('full_name'), str)
            or base_repo['full_name'].casefold() != repo.casefold()):
        raise GVError('PR repository identity was not confirmed', 'unexpected_response')
    return result


def close_pull(client, repo, args):
    from .cli import require_public
    require_public(client, 'pr close')
    text = args.comment
    if text is not None and (not text.strip() or '\0' in text or len(text.encode('utf-8')) > 1048576):
        raise GVError('Closing comment must be nonblank UTF-8, without NUL, at most 1 MiB', 'usage_error')
    path = f'repos/{repo}/pulls/{args.number}'
    comments = f'repos/{repo}/issues/{args.number}/comments'
    if client.dry_run:
        return {'dry_run': True, 'repository': repo, 'number': args.number,
                'requests': ([{'method': 'POST', 'path': comments, 'body': {'body': text}}] if text is not None else [])
                            + [{'method': 'PATCH', 'path': path, 'body': {'state': 'closed'}}],
                'plan': 'Verify PR identity/state/revision; closed is no-op, merged refuses. '
                        'If open: comment once and GET by ID, recheck PR, close once, GET PR. No network.',
                'atomic': False, 'delete_branch': 'unimplemented; no local or remote branch deletion'}
    deadline = time.monotonic() + args.timeout

    def request(method, target, body=None):
        return client.request(method, target, body=body, deadline=deadline,
                              require_complete=True, max_response_bytes=1048576)

    before = request('GET', path)
    pinned = snapshot(before, repo, args.number)
    if before['merged']:
        raise GVError('Merged PR cannot be closed by this command', 'pr_merged')
    if before['state'] == 'closed':
        return {**before, 'changed': False, 'closing_comment_id': None}
    user = user_value(request('GET', 'user')) if text is not None else None
    current = snapshot(request('GET', path), repo, args.number)
    if current != pinned:
        raise GVError('PR changed before close; no mutation sent', 'concurrent_change')
    completed, comment_id, stage = [], None, 'comment' if text is not None else 'close'
    try:
        if text is not None:
            created = request('POST', comments, {'body': text})
            # Retain a valid recovery ID even when the rest of the DTO is bad.
            if isinstance(created, dict) and type(created.get('id')) is int and created['id'] > 0:
                comment_id = created['id']
            if comment_id is None:
                raise GVError('Comment creation outcome is unknown; inspect discussion', 'unexpected_response')
            stage = 'verify_comment'
            expected = client.base.rstrip('/') + f'/repos/{repo}/issues/{args.number}'
            actual = comment_snapshot(request('GET', f'repos/{repo}/issues/comments/{comment_id}'),
                                      comment_id, expected, user['id'])
            if actual['body'] != text:
                raise GVError('Closing comment body was not confirmed', 'verification_failed')
            completed.append('comment')
            stage = 'recheck'
            # A comment may update updated_at/comments. All other pinned fields
            # must still match; this is a best-effort guard, not atomic CAS.
            latest = snapshot(request('GET', path), repo, args.number)
            if {k: v for k, v in latest.items() if k != 'updated_at'} != {k: v for k, v in pinned.items() if k != 'updated_at'}:
                raise GVError('PR changed after comment; close was not sent', 'concurrent_change')
        stage = 'close'
        request('PATCH', path, {'state': 'closed'})
        stage = 'verify_close'
        after = request('GET', path)
        actual = snapshot(after, repo, args.number)
        if (actual['state'] != 'closed' or actual['merged']
                or any(actual[k] != pinned[k] for k in ('id', 'number', 'head_sha', 'base_sha', 'head', 'base'))):
            raise GVError('Closed state/identity was not confirmed', 'verification_failed')
        completed.append('close')
        return {**after, 'changed': True, 'closing_comment_id': comment_id,
                'completed_steps': completed, 'close_verification': {'readback': True, 'atomic': False}}
    except GVError as error:
        error.details = {**(error.details or {}), 'repository': repo, 'pull_number': args.number,
                         'stage': stage, 'completed_steps': completed,
                         'closing_comment_id': comment_id, 'mutation_attempted': True,
                         'recovery': 'Read PR and discussion/comment ID before retrying. '
                                     'A comment or close may have applied. No automatic retry or rollback.'}
        raise
