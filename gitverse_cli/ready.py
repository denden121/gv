"""GitVerse derives PR draft state from its title, as its frontend does."""
import re

from .client import GVError


DRAFT_PREFIX = re.compile(r'^draft\s?:', re.IGNORECASE)


def inspect_pull(pull, number):
    if not isinstance(pull, dict) or type(pull.get('number')) is not int or pull['number'] != number:
        raise GVError('Unexpected PR identity', 'unexpected_response')
    if type(pull.get('draft')) is not bool or not isinstance(pull.get('title'), str) or not pull['title'].strip():
        raise GVError('PR response needs a non-empty title and boolean draft', 'unexpected_response')
    if pull.get('state') != 'open' or pull.get('merged') is True:
        raise GVError('Only open, unmerged PRs can change draft state; no PR was reopened', 'pr_not_open')
    return pull['title'], pull['draft']


def pr_ready(args, client, repo, number):
    from .cli import require_public
    require_public(client, 'pr ready')
    path = f'repos/{repo}/pulls/{number}'
    desired = bool(args.undo)
    if client.dry_run:
        return {'repository': repo, 'number': number, 'requested_draft': desired,
                'read_request': client.request('GET', path),
                'mutation_plan': {'method': 'PATCH', 'path': path, 'body_field': 'title',
                                  'transform': 'Prepend Draft: if not already draft' if desired else 'Remove the leading Draft: prefix and trim',
                                  'conditions': 'Open, unmerged PR; unchanged title/state before write; already requested state is a no-op'},
                'verification': 'GET PR and require exact title, requested boolean draft, and open/unmerged state. No draft/state field is sent.'}
    pull = client.request('GET', path)
    title, draft = inspect_pull(pull, number)
    if draft == desired:
        return {'repository': repo, 'number': number, 'draft': draft, 'title': title, 'changed': False, 'pull_request': pull}
    prefix = DRAFT_PREFIX.match(title)
    if bool(prefix) != draft:
        raise GVError('Draft state and the supported leading Draft: title prefix disagree; inspect PR before editing', 'unsupported_draft_title')
    updated_title = 'Draft: ' + title if desired else title[prefix.end():].strip()
    if not updated_title.strip() or (not desired and DRAFT_PREFIX.match(updated_title)):
        raise GVError('Removing one Draft: prefix leaves an empty or still-draft title; edit the title explicitly', 'unsupported_draft_title')
    # The API has no documented atomic compare-and-swap for titles. Detect
    # changes we can observe, and never retry a PATCH or a verification failure.
    latest = client.request('GET', path)
    latest_title, latest_draft = inspect_pull(latest, number)
    if (latest_title, latest_draft) != (title, draft):
        raise GVError('PR title/draft changed before the update; no mutation was sent', 'pr_changed')
    stage = 'update'
    try:
        client.request('PATCH', path, body={'title': updated_title})
        stage = 'verify'
        result = client.request('GET', path)
        actual_title, actual_draft = inspect_pull(result, number)
        if actual_title != updated_title or actual_draft != desired:
            raise GVError('Server did not confirm the requested title and draft state', 'ready_not_confirmed')
    except GVError as error:
        details = dict(error.details or {})
        details.update({'stage': stage, 'mutation_attempted': True, 'requested_draft': desired,
                        'recovery': 'Read the PR before retrying: the title update may already have applied.'})
        raise GVError(str(error), error.code, error.status, details) from error
    return {'repository': repo, 'number': number, 'draft': actual_draft, 'title': actual_title, 'changed': True, 'pull_request': result}
