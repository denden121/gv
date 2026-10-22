"""One-shot deletion of the newest own public PR discussion comment."""
import http.client

from .client import GVError
from .comment_edit import comment_snapshot
from .directory import user_value
from .issues import Session
from .pr_close import snapshot
from .pr_comment import newest_own, validate_mode


def delete_last(client, repo, args):
    from .cli import require_public
    require_public(client, 'pr comment --delete-last')
    validate_mode(args)
    root = 'repos/' + repo
    parent = root + '/pulls/' + str(args.number)
    catalog = root + '/issues/' + str(args.number) + '/comments'
    expected = client.base.rstrip('/') + '/' + root + '/issues/' + str(args.number)
    identity = {'repository': repo, 'number': args.number}
    cascade = 'GitVerse deletes all replies when the selected comment is a thread parent; the public DTO cannot prove absence of replies.'
    if client.dry_run:
        return {**identity, 'dry_run': True, 'backend': 'public', 'method': 'select DELETE',
                'path': catalog + '/{selected_own_comment_id}', 'delete_last': True,
                'cascade': cascade,
                'plan': 'No network. Verify PR/current user, read complete discussion catalog twice, select newest own created_at then greatest ID, verify selected comment and PR, DELETE once with --yes; require HTTP204 then independent GET404. No atomic CAS or retries. Absence is an error.'}
    session = Session(client, args)

    def request(method, path, **kwargs):
        try:
            return client.request(method, path, deadline=session.deadline, require_complete=True,
                                  max_response_bytes=33554432, **kwargs)
        except (OSError, http.client.HTTPException) as error:
            raise GVError('Discussion transport failed; reconcile recorded ID before retrying', 'network_error',
                          details={**identity, 'mutation_attempted': False}) from error

    pinned = snapshot(request('GET', parent), repo, args.number)
    uid = user_value(request('GET', 'user'))['id']
    before = newest_own(request, catalog, expected, uid, args.max_entries, session.deadline)
    if before is None:
        raise GVError('No own discussion comment found; no deletion sent', 'comment_not_found',
                      details={**identity, 'mutation_attempted': False})
    identity['comment_id'] = before['id']
    read_path = root + '/issues/comments/' + str(before['id'])
    if comment_snapshot(request('GET', read_path), before['id'], expected, uid) != before:
        raise GVError('Selected comment changed; no deletion sent', 'concurrent_change',
                      details={**identity, 'mutation_attempted': False})
    if (newest_own(request, catalog, expected, uid, args.max_entries, session.deadline) != before or
            snapshot(request('GET', parent), repo, args.number) != pinned):
        raise GVError('Newest comment or PR changed; no deletion sent', 'concurrent_change',
                      details={**identity, 'mutation_attempted': False})
    stage = 'delete'
    try:
        result = request('DELETE', catalog + '/' + str(before['id']), expected_status=204)
        if result is not None:
            raise GVError('Expected empty HTTP204 deletion response', 'unexpected_response')
        stage = 'verify'
        try:
            request('GET', read_path)
        except GVError as error:
            if error.code != 'http_error' or error.status != 404:
                raise
        else:
            raise GVError('Deleted comment remains visible', 'deletion_not_confirmed')
        return {**identity, 'changed': True, 'deleted': True, 'deletion_accepted': True,
                'cascade': cascade, 'verification': {'delete_status': 204, 'readback_status': 404,
                    'atomic_compare_and_swap': False,
                    'scope': 'Selected canonical comment path absent after accepted deletion; reply preservation is not guaranteed.'}}
    except GVError as error:
        error.details = {**identity, 'stage': stage, 'mutation_attempted': True,
                         'deletion_accepted': stage == 'verify',
                         'recovery': 'Read this comment ID and parent discussion before deciding recovery. Never blindly repeat --delete-last: it could delete the preceding comment. No automatic retry or rollback.'}
        raise
