"""Verified public PR discussion creation and own-last editing."""
import http.client
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from .actions_config import read_value
from .client import GVError
from .comment_edit import comment_snapshot
from .directory import user_value
from .issues import Session
from .pr_close import snapshot


def newest_own(request, path, expected_url, user_id, maximum, deadline):
    # The public schema promises all comments, with since/before only. Do not
    # invent pagination or silently accept a truncated list.
    rows = request('GET', path)
    if not isinstance(rows, list): raise GVError('Unknown discussion catalog', 'unexpected_response')
    if len(rows) > maximum: raise GVError('Discussion entry limit reached; selection refused', 'catalog_limit')
    seen, candidates = set(), []
    for row in rows:
        if time.monotonic() >= deadline:
            raise GVError('Discussion selection deadline reached', 'deadline_exceeded')
        if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0:
            raise GVError('Missing discussion identity', 'unexpected_response')
        if row['id'] in seen: raise GVError('Duplicate discussion ID; selection refused', 'pagination_error')
        seen.add(row['id'])
        author = row.get('user')
        if not isinstance(author, dict) or type(author.get('id')) is not int or author['id'] <= 0:
            raise GVError('Unknown discussion author', 'unexpected_response')
        # Validate every parent reference without following the returned URL.
        snapshot = comment_snapshot(row, row['id'], expected_url, author['id'])
        if author['id'] != user_id: continue
        try:
            stamp = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00'))
            if stamp.tzinfo is None: raise ValueError()
            stamp = stamp.astimezone(timezone.utc)
        except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
            raise GVError('Own discussion creation time is unknown', 'unexpected_response') from None
        candidates.append((stamp, row['id'], snapshot))
    return max(candidates, key=lambda item: item[:2])[2] if candidates else None


def validate_mode(args):
    if getattr(args, 'web', False) and getattr(args, 'backend', 'public') != 'public':
        raise GVError('--web uses public PAT preflight, never the cookie API backend', 'backend_unsupported')
    if getattr(args, 'web', False) and (
            args.body is not None or args.body_file is not None or
            getattr(args, 'editor', False) or getattr(args, 'delete_last', False) or
            args.reply_to or args.path is not None or args.line is not None or
            args.attach or args.commit):
        raise GVError('--web cannot combine with body/body-file/editor/delete-last/attachment/inline/reply/commit', 'usage_error')
    editor = getattr(args, 'editor', False)
    if editor and (args.body is not None or args.body_file is not None or
                   getattr(args, 'delete_last', False) or args.reply_to or
                   args.path is not None or args.line is not None or args.commit):
        raise GVError('--editor requires discussion mode without --body/--body-file/--delete-last/inline/reply/commit', 'usage_error')
    delete_last = getattr(args, 'delete_last', False)
    if getattr(args, 'yes', False) and not delete_last:
        raise GVError('--yes requires --delete-last', 'usage_error')
    if delete_last:
        if (getattr(args, 'edit_last', False) or getattr(args, 'create_if_none', False) or
                args.body is not None or args.body_file is not None or args.reply_to or
                args.path is not None or args.line is not None or args.attach or args.commit):
            raise GVError('--delete-last cannot combine with text, edit, creation, attachment, inline or reply modes', 'usage_error')
        if not getattr(args, 'yes', False) and not getattr(args, 'dry_run', False):
            raise GVError('--delete-last requires --yes; use --dry-run to preview without confirmation or network', 'usage_error')
    edit_last = getattr(args, 'edit_last', False)
    create_if_none = getattr(args, 'create_if_none', False)
    if create_if_none and not edit_last:
        raise GVError('--create-if-none requires --edit-last', 'usage_error')
    if edit_last and (args.reply_to or args.path is not None or args.line is not None or args.attach):
        raise GVError('--edit-last/--create-if-none require discussion text without attachments, inline comments or replies', 'usage_error')
    if args.commit and (edit_last or not (args.reply_to or args.path is not None or args.line is not None)):
        raise GVError('--commit applies only to inline comments', 'usage_error')

def discussion_comment(client, repo, args):
    from .cli import require_public
    require_public(client, 'pr comment')
    validate_mode(args)
    edit_last = getattr(args, 'edit_last', False)
    create_if_none = getattr(args, 'create_if_none', False)
    session = Session(client, args)
    editor = getattr(args, 'editor', False)
    from .comment_attachments import prepare, render, multipart, attachment_list, asset_value
    assets = prepare(args.attach, session.deadline) if getattr(args, 'attach', None) else []
    if editor and client.dry_run:
        return {'repository': repo, 'number': args.number, 'dry_run': True, 'backend': 'public',
                'editor': True, 'edit_last': edit_last, 'create_if_none': create_if_none,
                'body_known': False, 'editor_launched': False,
                'plan': 'No editor, draft, network or write. Verify PR/user; select and seed newest own body for edit-last, empty draft for creation/fallback. Open editor without prompts; validate draft, recheck snapshots, write at most once and independently GET. Absence refuses unless create-if-none; equal edit is no-op. No atomic CAS/retry.'}
    text = (read_value(SimpleNamespace(value_file=args.body_file, max_bytes=1048576, group='pr'), session.deadline)
            if args.body_file is not None else args.body)
    if text is None and (assets or editor): text = ''
    try:
        body_size = len(text.encode('utf-8')) if isinstance(text, str) else None
    except UnicodeEncodeError:
        raise GVError('Comment body must be valid UTF-8', 'usage_error') from None
    if not editor and (body_size is None or (not text.strip() and not assets) or '\0' in text or body_size > 1048576):
        raise GVError('Supply nonblank UTF-8 --body or --body-file FILE/- up to 1 MiB without NUL', 'usage_error')
    if assets:
        # Check rendering before publishing anything. Placeholders never claim
        # to be real uploaded URLs, including in offline previews.
        preview = render(text, assets, ['https://attachment.invalid/' + str(i) for i in range(len(assets))], args.body_file)
    initial_text = text if text.strip() else 'Attachment upload pending.'
    root = 'repos/' + repo
    parent = root + '/pulls/' + str(args.number)
    path = root + '/issues/' + str(args.number) + '/comments'
    expected_url = client.base.rstrip('/') + '/' + root + '/issues/' + str(args.number)
    identity = {'repository': repo, 'number': args.number}
    if client.dry_run:
        if edit_last:
            fallback_plan = ('Otherwise absence permits one POST.' if create_if_none else
                             'Absence is comment_not_found; never create.')
            return {**identity, 'dry_run': True, 'backend': 'public', 'path': path,
                    'method': 'select PATCH or POST' if create_if_none else 'select PATCH',
                    'body': {'body': text}, 'edit_last': True, 'create_if_none': create_if_none,
                    'plan': 'No network. Read complete documented discussion catalog twice; select newest own creation time then ID. Recheck PR and comment. Equal text is no-op; otherwise PATCH once. ' + fallback_plan + ' Independently GET exact text/author/parent. No atomic CAS or retries.'}
        if assets:
            return {**identity, 'dry_run': True, 'backend': 'public', 'path': path,
                    'body': {'body': text}, 'preview_with_placeholder_urls': preview,
                    'attachments': [{k: a[k] for k in ('name', 'type', 'sha256')} | {'size': len(a['data'])} for a in assets],
                    'plan': 'No network. Create and GET own discussion once; POST each file to its comment attachment route and independently GET metadata; recheck body/version/PR; PATCH rendered body once, then GET. URLs are unknown until upload. Partial writes are possible; no retries/rollback/CAS.'}
        return {**identity, 'dry_run': True, 'method': 'POST', 'path': path,
                'url': client.base.rstrip('/') + '/' + path, 'backend': 'public', 'body': {'body': text},
                'plan': 'Verify PR repository/revision and current user; recheck PR; POST once and GET comment by ID. No network. Each invocation creates a comment; no deduplication or atomic CAS.'}
    def request(method, target, body=None, **kwargs):
        try:
            return client.request(method, target, body=body, deadline=session.deadline,
                                  require_complete=True, max_response_bytes=33554432, **kwargs)
        except (OSError, http.client.HTTPException) as error:
            # Socket resets and broken response streams can occur after the
            # server applied a POST. Never expose transport exception text or
            # retry here; the mutation guard below attaches recovery identity.
            raise GVError('Discussion transport failed; inspect the comment/discussion before retrying',
                          'network_error', details={**identity, 'mutation_attempted': False}) from error

    pinned = snapshot(request('GET', parent), repo, args.number)
    user_id = user_value(request('GET', 'user'))['id']
    before = newest_own(request, path, expected_url, user_id, args.max_entries, session.deadline) if edit_last else None
    if edit_last and before is None and not create_if_none:
        raise GVError('No own discussion comment found; use --create-if-none to create', 'comment_not_found',
                      details={**identity, 'mutation_attempted': False})
    if before:
        identity['comment_id'] = before['id']
        read_path = root + '/issues/comments/' + str(before['id'])
        if comment_snapshot(request('GET', read_path), before['id'], expected_url, user_id) != before:
            raise GVError('Selected comment changed; no write sent', 'concurrent_change',
                          details={**identity, 'mutation_attempted': False})
    if editor:
        from .comment_editor import edit_text
        text = edit_text(before['body'] if before else '', session.deadline)
        initial_text = text
        if assets:
            render(text, assets, ['https://attachment.invalid/' + str(i) for i in range(len(assets))], None)
    if edit_last and newest_own(request, path, expected_url, user_id, args.max_entries, session.deadline) != before:
        raise GVError('Newest own comment changed; no write sent', 'concurrent_change',
                      details={**identity, 'mutation_attempted': False})
    if snapshot(request('GET', parent), repo, args.number) != pinned:
        raise GVError('PR changed before comment; no write sent', 'concurrent_change',
                      details={**identity, 'mutation_attempted': False})
    if before and before['body'] == text:
        return {**identity, 'changed': False, 'body': text,
                'verification': {'readback': True, 'atomic_compare_and_swap': False}}
    if before:
        stage = 'edit'
        try:
            request('PATCH', path + '/' + str(before['id']), body={'body': text})
            stage = 'verify'
            actual = comment_snapshot(request('GET', read_path), before['id'], expected_url, user_id)
            if actual['body'] != text:
                raise GVError('Edited comment text not confirmed by GET', 'verification_failed')
            return {**identity, 'changed': True, 'body': text,
                    'verification': {'readback': True, 'atomic_compare_and_swap': False}}
        except GVError as error:
            error.details = {**(error.details or {}), **identity, 'stage': stage, 'mutation_attempted': True,
                             'recovery': 'Read the recorded comment ID before retrying. No automatic write retry/rollback or atomic CAS.'}
            raise
    stage = 'create'
    try:
        row = request('POST', path, body={'body': initial_text})
        if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0:
            raise GVError('Created comment ID unknown; inspect discussion before retry', 'unexpected_response')
        identity['comment_id'] = row['id']
        comment_snapshot(row, row['id'], expected_url, user_id)
        stage = 'verify'
        row = request('GET', root + '/issues/comments/' + str(identity['comment_id']))
        actual = comment_snapshot(row, identity['comment_id'], expected_url, user_id)
        if actual['body'] != initial_text:
            raise GVError('Written comment text not confirmed by GET', 'verification_failed')
        if assets:
            read_path = root + '/issues/comments/' + str(identity['comment_id'])
            if attachment_list(row):
                raise GVError('New comment already has attachments', 'concurrent_change')
            identity['attachment_ids'] = []
            confirmed = []
            for i, asset in enumerate(assets):
                identity['attachment_index'] = i
                stage = 'upload_attachment'
                wire, content_type = multipart(asset)
                uploaded = request('POST', read_path + '/attachments', encoded_body=wire, content_type=content_type, expected_status=201)
                # Preserve every usable recovery ID even if another field is malformed.
                if isinstance(uploaded, list):
                    identity['returned_attachment_ids'] = [a['id'] for a in uploaded if isinstance(a, dict) and type(a.get('id')) is int and a['id'] > 0]
                if not isinstance(uploaded, list) or not 1 <= len(uploaded) <= 50:
                    raise GVError('Unknown attachment upload result', 'unexpected_response')
                received = [asset_value(a) for a in uploaded]
                new = [a for a in received if a['id'] not in identity['attachment_ids']]
                if len(new) != 1 or new[0]['name'] != asset['name'] or new[0]['size'] != len(asset['data']):
                    raise GVError('Uploaded attachment name/size/identity not confirmed', 'verification_failed')
                confirmed.append(new[0]); identity['attachment_ids'].append(new[0]['id'])
                stage = 'verify_attachment'
                row = request('GET', read_path)
                current = comment_snapshot(row, identity['comment_id'], expected_url, user_id)
                if current['body'] != initial_text or attachment_list(row) != confirmed:
                    raise GVError('Comment or attachment metadata changed during upload', 'concurrent_change')
            rendered = render(text, assets, [a['browser_download_url'] for a in confirmed], args.body_file)
            stage = 'preflight_body'
            if snapshot(request('GET', parent), repo, args.number) != pinned:
                raise GVError('PR changed during attachment upload', 'concurrent_change')
            before = comment_snapshot(row, identity['comment_id'], expected_url, user_id)
            row = request('GET', read_path)
            if comment_snapshot(row, identity['comment_id'], expected_url, user_id) != before or attachment_list(row) != confirmed:
                raise GVError('Comment changed before final text update', 'concurrent_change')
            stage = 'update_body'
            request('PATCH', path + '/' + str(identity['comment_id']), body={'body': rendered})
            stage = 'verify_body'
            row = request('GET', read_path)
            if comment_snapshot(row, identity['comment_id'], expected_url, user_id)['body'] != rendered or attachment_list(row) != confirmed:
                raise GVError('Final attachment comment text/metadata not confirmed', 'verification_failed')
        return row  # Keep the existing creation DTO interface.
    except GVError as error:
        error.details = {**(error.details or {}), **identity, 'stage': stage, 'mutation_attempted': True,
                         'recovery': 'Read the recorded comment ID or parent discussion before retrying. No automatic write retry/rollback; creation without an ID may have applied.'}
        raise
