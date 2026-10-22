"""One-shot explicit repository deletion with identity and readback checks."""
from .client import GVError
from .issues import Session
from .repo_lifecycle import state


def validate_delete_args(args):
    if not getattr(args, 'repo', None):
        raise GVError('repo delete requires explicit -R OWNER/REPO; defaults/remotes are not used', 'usage_error')
    if not getattr(args, 'yes', False) and not getattr(args, 'dry_run', False):
        raise GVError('repo delete is irreversible; pass --yes explicitly, or --dry-run to preview', 'usage_error')


def inspect(row, repo, match_id):
    snapshot = state(row, repo)
    permissions = row.get('permissions')
    if not isinstance(permissions, dict) or permissions.get('admin') is not True:
        raise GVError('Repository admin permission was not confirmed; no deletion sent', 'permission_not_confirmed')
    if match_id is not None and snapshot['id'] != match_id:
        raise GVError('Repository ID differs from --match-id; no deletion sent', 'repository_id_mismatch',
                      details={'repository': repo, 'actual_id': snapshot['id'], 'expected_id': match_id, 'mutation_attempted': False})
    return snapshot


def delete_repository(client, repo, args):
    validate_delete_args(args)
    path = 'repos/' + repo
    if client.dry_run:
        return {'dry_run': True, 'repository': repo, 'match_id': args.match_id, 'method': 'DELETE', 'path': path,
                'plan': 'Verify canonical identity/admin permission and optional ID; recheck identity/state; DELETE once expecting HTTP204; require following GET404. No redirects, retries, local cleanup or network.'}
    session = Session(client, args)
    before = inspect(session.request('GET', path), repo, args.match_id)
    current = inspect(session.request('GET', path), repo, args.match_id)
    if current != before:
        raise GVError('Repository identity/state changed before deletion; no deletion sent', 'concurrent_change',
                      details={'repository': repo, 'mutation_attempted': False})
    stage = 'delete'
    try:
        result = client.request('DELETE', path, deadline=session.deadline, require_complete=True,
                                max_response_bytes=33554432, expected_status=204)
        if result is not None: raise GVError('Expected an empty HTTP204 deletion response', 'unexpected_response')
        stage = 'verify'
        try: session.request('GET', path)
        except GVError as error:
            if error.code != 'http_error' or error.status != 404: raise
        else: raise GVError('Repository remains visible after deletion', 'deletion_not_confirmed')
        return {'repository': before['full_name'], 'repository_id': before['id'], 'deleted': True,
                'deletion_accepted': True, 'verification': {'delete_status': 204, 'readback_status': 404,
                    'scope': 'Canonical API path absent after accepted deletion; not a global name-availability claim',
                    'atomic_compare_and_swap': False}, 'local_cleanup_performed': False}
    except GVError as error:
        error.details = {'repository': before['full_name'], 'repository_id': before['id'], 'stage': stage,
                         'mutation_attempted': True,
                         'recovery': 'Read the explicit repository path before retrying; missing/inaccessible does not imply a safe retry. No automatic DELETE retries or rollback.'}
        raise
