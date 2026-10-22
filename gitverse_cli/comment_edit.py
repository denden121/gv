"""Explicit-ID own discussion edits with public parent/author/body checks."""
from urllib.parse import urlsplit

from .client import GVError
from .directory import user_value
from .issues import Session, issue_value


def parent_identity(row, number, kind):
    issue_value(row, number, allow_pull=kind == 'pr')
    if type(row.get('id')) is not int or row['id'] <= 0:
        raise GVError('Discussion parent identity is missing', 'unexpected_response')
    return row['id']


def comment_snapshot(row, comment_id, expected_url, user_id):
    if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] != comment_id:
        raise GVError('Unexpected discussion comment identity', 'unexpected_response')
    author = row.get('user')
    if not isinstance(author, dict) or type(author.get('id')) is not int or author['id'] <= 0:
        raise GVError('Comment author identity is unknown', 'unexpected_response')
    if author['id'] != user_id:
        raise GVError('Only your own discussion comments can be edited by this command', 'comment_not_owned')
    if not isinstance(row.get('issue_url'), str): raise GVError('Comment parent reference is missing', 'unexpected_response')
    try: actual, expected = urlsplit(row['issue_url']), urlsplit(expected_url)
    except ValueError: raise GVError('Invalid comment parent reference', 'unexpected_response') from None
    if actual.username is not None or actual.password is not None or actual.query or actual.fragment or actual.scheme != expected.scheme or actual.netloc.casefold() != expected.netloc.casefold() or actual.path.rstrip('/').casefold() != expected.path.casefold():
        raise GVError('Comment does not belong to the selected repository and issue/PR', 'comment_parent_mismatch')
    if not isinstance(row.get('body'), str) or not isinstance(row.get('updated_at'), str) or not row['updated_at']:
        raise GVError('Comment body/version metadata is missing', 'unexpected_response')
    return {'id': row['id'], 'author_id': author['id'], 'body': row['body'], 'updated_at': row['updated_at']}


def edit_comment(client, repo, args):
    from .cli import body_text
    text = body_text(args, required=True)
    if '\0' in text or len(text.encode('utf-8')) > 1048576:
        raise GVError('Comment text must not contain NUL or exceed 1 MiB UTF-8', 'usage_error')
    root = 'repos/' + repo
    parent = root + ('/pulls/' if args.group == 'pr' else '/issues/') + str(args.number)
    read_path = root + '/issues/comments/' + str(args.comment_id)
    write_path = root + f'/issues/{args.number}/comments/{args.comment_id}'
    expected_url = client.base.rstrip('/') + '/' + root + '/issues/' + str(args.number)
    if client.dry_run:
        return {'dry_run': True, 'repository': repo, 'number': args.number, 'comment_id': args.comment_id,
                'method': 'PATCH', 'path': write_path, 'body': {'body': text},
                'plan': 'Verify parent/current-user/comment ownership and issue_url; skip equal body; recheck parent/comment; PATCH once and verify GET. Discussion comments only; no network.'}
    session = Session(client, args)
    parent_id = parent_identity(session.request('GET', parent), args.number, args.group)
    user = user_value(session.request('GET', 'user'))
    before = comment_snapshot(session.request('GET', read_path), args.comment_id, expected_url, user['id'])
    identity = {'repository': repo, 'number': args.number, 'comment_id': args.comment_id}
    if before['body'] == text:
        return {**identity, 'changed': False, 'body': text, 'verification': {'readback': True, 'atomic_compare_and_swap': False}}
    latest = comment_snapshot(session.request('GET', read_path), args.comment_id, expected_url, user['id'])
    current_parent = parent_identity(session.request('GET', parent), args.number, args.group)
    if latest != before or current_parent != parent_id:
        raise GVError('Comment or parent changed before edit; no PATCH sent', 'concurrent_change',
                      details={**identity, 'mutation_attempted': False})
    stage = 'edit'
    try:
        session.request('PATCH', write_path, body={'body': text})
        stage = 'verify'
        actual = comment_snapshot(session.request('GET', read_path), args.comment_id, expected_url, user['id'])
        if actual['body'] != text: raise GVError('Edited comment body was not confirmed by GET', 'verification_failed')
        return {**identity, 'changed': True, 'body': actual['body'],
                'verification': {'readback': True, 'atomic_compare_and_swap': False}}
    except GVError as error:
        error.details = {**identity, 'stage': stage, 'mutation_attempted': True,
                         'recovery': 'Read the explicit comment ID before retrying; no automatic PATCH retries or rollback'}
        raise
