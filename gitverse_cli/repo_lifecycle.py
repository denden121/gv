"""Verified public archive/unarchive/rename; redirects never carry credentials."""
from .client import GVError
from .issues import Session
from .repositories import component, identity


def state(row, repo):
    identity(row, repo)
    if type(row.get('archived')) is not bool:
        raise GVError('Repository archive state is unknown', 'unexpected_response')
    return {k: row[k] for k in ('id', 'full_name', 'private', 'archived')}


def checked(session, path, repo):
    return state(session.request('GET', path), repo)


def lifecycle(client, repo, args):
    rename = args.action == 'rename'
    name = component(args.new_name) if rename else None
    destination = repo.split('/')[0] + '/' + name if rename else repo
    requested = {'name': name} if rename else {'archived': args.action == 'archive'}
    path = 'repos/' + repo
    if client.dry_run:
        return {'dry_run': True, 'action': args.action, 'repository': repo, 'destination': destination,
                'method': 'PATCH', 'path': path, 'body': requested,
                'plan': 'Read identity/archive state; noop if equal; rename checks destination visibility; recheck source; PATCH once; verify requested state at explicit destination. Never follow redirects.'}
    session = Session(client, args)
    before = checked(session, path, repo)
    if rename:
        destination = before['full_name'].split('/')[0] + '/' + name
        noop = destination.casefold() == before['full_name'].casefold()
        if before['archived'] and not noop:
            raise GVError('Unarchive this repository before renaming it', 'repo_archived')
    else: noop = before['archived'] == requested['archived']
    if noop:
        return {'action': args.action, 'repository': before['full_name'], 'repository_id': before['id'],
                'archived': before['archived'], 'changed': False, 'private': before['private'],
                'verification': {'readback': True, 'atomic_compare_and_swap': False}}
    destination_path = 'repos/' + destination
    if rename:
        try: target = session.request('GET', destination_path)
        except GVError as error:
            if error.code != 'http_error' or error.status not in (404, 301, 307): raise
            # A former name may redirect. Do not follow it or infer vacancy;
            # the server must enforce name availability on the single PATCH.
        else:
            if not isinstance(target, dict) or target.get('id') != before['id']:
                raise GVError('Rename destination is already visible; no mutation sent', 'destination_exists')
            if state(target, before['full_name']) != before:
                raise GVError('Destination alias identity/state is inconsistent', 'unexpected_response')
    current = checked(session, path, repo)
    if current != before:
        raise GVError('Repository identity/archive state changed before mutation', 'concurrent_change',
                      details={'repository': repo, 'mutation_attempted': False})
    stage, redirect_status = 'write', None
    try:
        try: session.request('PATCH', path, body=requested)
        except GVError as error:
            if not rename or error.code != 'http_error' or error.status != 307: raise
            # Contract permits 307 after rename. Do not read or follow Location.
            # Only our explicitly computed same-namespace target is read next.
            redirect_status = 307
        stage = 'verify'
        actual = checked(session, destination_path, destination)
        expected_archive = before['archived'] if rename else requested['archived']
        if actual['id'] != before['id'] or actual['private'] != before['private'] or actual['archived'] != expected_archive:
            raise GVError('Repository lifecycle outcome was not confirmed by identity/state readback', 'verification_failed')
        result = {'action': args.action, 'repository': actual['full_name'], 'previous_repository': before['full_name'],
                  'repository_id': actual['id'], 'archived': actual['archived'], 'private': actual['private'], 'changed': True,
                  'verification': {'readback': True, 'atomic_compare_and_swap': False, 'redirect_followed': False}}
        if rename:
            result.update(local_remotes_updated=False, rename_redirect_status=redirect_status)
        return result
    except GVError as error:
        error.details = {'action': args.action, 'repository': before['full_name'], 'destination': destination,
                         'repository_id': before['id'], 'stage': stage, 'mutation_attempted': True,
                         'recovery': 'Inspect the source and explicit destination with repo view before retrying; no automatic rollback or mutation retries'}
        raise
