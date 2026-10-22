"""Single public PR creation with bounded transport and recoverable readback."""
import http.client
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

from .actions_config import read_value
from .client import GVError
from .directory import user_value
from .repositories import identity
from .ready import DRAFT_PREFIX


def positive_id(value):
    return type(value) is int and value > 0


def branch(value):
    try:
        size = len(value.encode('utf-8')) if isinstance(value, str) else 0
    except UnicodeError:
        raise GVError('Branch must be valid UTF-8', 'usage_error') from None
    if (not isinstance(value, str) or not value or value == '@' or size > 1024
            or value.startswith(('-', '/')) or value.endswith(('/', '.', '.lock'))
            or '..' in value or '@{' in value or '//' in value
            or any(ord(c) < 33 or ord(c) == 127 or c in '~^:?*[\\' for c in value)
            or any(part.startswith('.') or part.endswith('.lock') for part in value.split('/'))):
        raise GVError('Use a valid branch name', 'usage_error')
    return value


def select_head(value, repo, deadline):
    """Explicit input never falls back to Git, including the empty string."""
    if value is None:
        from .pr_create_fill import git_read
        value = git_read(['symbolic-ref', '--quiet', '--short', 'HEAD'], deadline).removesuffix('\n')
    owner = None
    if ':' in value:
        from .directory import login
        parts = value.split(':')
        if len(parts) != 2:
            raise GVError('Use BRANCH or USER:BRANCH for --head', 'usage_error')
        owner, value = login(parts[0]), parts[1]
        branch(value)
    return branch(value), owner


def validate_title(title, allow_blank=False):
    try:
        valid = (isinstance(title, str) and (allow_blank or bool(title.strip()))
                 and len(title.encode('utf-8')) <= 1048576
                 and not any(c in title for c in ('\0', '\r', '\n')))
    except UnicodeError:
        valid = False
    if not valid:
        raise GVError('Title must be a nonblank single line, at most 1 MiB UTF-8; pass --title, a fill option or --editor', 'usage_error')


def validate_body(text):
    if text is None:
        return
    try:
        valid = isinstance(text, str) and '\0' not in text and len(text.encode('utf-8')) <= 1048576
    except UnicodeError:
        valid = False
    if not valid:
        raise GVError('Body must be UTF-8 without NUL, at most 1 MiB', 'usage_error')


def configured_base(deadline):
    """Use the current checkout branch's gh config, even with explicit --head.

    An explicit --base bypasses this lookup. Outside Git, or on a detached
    HEAD, there is no current branch setting; repository default is next.
    Read the effective Git configuration (including its normal includes).
    """
    from .repo_default import command, context
    if context(deadline=deadline) is None:
        cwd = Path.cwd()
        if (any(key in os.environ for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR'))
                or any(os.path.lexists(parent / '.git') for parent in (cwd, *cwd.parents))):
            raise GVError('Cannot read current Git context for gh-merge-base', 'git_error')
        return None
    current = command('symbolic-ref', '--quiet', '--short', 'HEAD',
                      allowed=(0, 1), deadline=deadline)
    if current.returncode == 1:
        return None
    name = branch(current.stdout.removesuffix('\n'))
    value = command('config', '--null', '--get', f'branch.{name}.gh-merge-base',
                    allowed=(0, 1), deadline=deadline)
    if value.returncode == 1:
        return None
    if not value.stdout.endswith('\0'):
        raise GVError('Invalid gh-merge-base Git config result', 'git_error')
    return branch(value.stdout[:-1])


def validate_pull(row, payload, repo, repo_id, author, number=None, source=None):
    if (not isinstance(row, dict) or not positive_id(row.get('id'))
            or not positive_id(row.get('number')) or (number is not None and row['number'] != number)):
        raise GVError('Created PR identity was not confirmed', 'unexpected_response')
    if (row.get('state') != 'open' or row.get('merged') is not False
            or type(row.get('draft')) is not bool or row['draft'] != payload['draft']
            or row.get('title') != payload['title'] or row.get('body') != payload.get('body', '')
            or user_value(row.get('user'))['id'] != author['id']
            or row['user']['login'].casefold() != author['login'].casefold()):
        raise GVError('Created PR text/state/author was not confirmed', 'verification_failed')
    pins = [row['id'], row['number']]
    if ('maintainer_can_modify' in payload
            and row.get('maintainer_can_modify') is not payload['maintainer_can_modify']):
        raise GVError('Created PR maintainer branch permission was not confirmed', 'verification_failed')
    for key in ('base', 'head'):
        value = row.get(key)
        if not isinstance(value, dict) or value.get('ref') != payload[key]:
            raise GVError('Created PR branch was not confirmed', 'verification_failed')
        expected_repo = source['repository'] if source and key == 'head' else repo
        expected_id = source['id'] if source and key == 'head' else repo_id
        nested = value.get('repo')
        if (not isinstance(nested, dict) or not isinstance(nested.get('full_name'), str) or nested['full_name'].casefold() != expected_repo.casefold()
                or type(nested.get('id')) is not int or nested['id'] != expected_id
                or (value.get('repo_id') is not None and (type(value['repo_id']) is not int or value['repo_id'] != expected_id))):
            raise GVError('Created PR repository scope was not confirmed', 'verification_failed')
        sha = value.get('sha')
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', sha):
            raise GVError('Created PR branch SHA is missing', 'unexpected_response')
        if source and sha.lower() != source[key + '_sha']:
            raise GVError('Created PR differs from selected source/base pins', 'concurrent_change')
        pins.append(sha.lower())
    return pins


class Bounded:
    def __init__(self, client, deadline):
        self.client, self.deadline = client, deadline

    def __getattr__(self, name):
        return getattr(self.client, name)

    def request(self, method, path, query=None, body=None):
        try:
            return self.client.request(method, path, query, body=body, deadline=self.deadline,
                                       require_complete=True, max_response_bytes=33554432,
                                       **({'expected_status': 201} if method == 'POST' and path.endswith('/pulls') else {}))
        except http.client.HTTPException as error:
            raise GVError('HTTP response interrupted; mutation outcome may be unknown', 'network_error') from error



def create_pull(client, repo, args):
    deadline = time.monotonic() + args.timeout
    template = getattr(args, 'template', None)
    if template is not None and (args.body is not None or args.body_file is not None):
        raise GVError('--template is not supported with --body or --body-file', 'usage_error')
    from .pr_create_recover import Recovery
    # Restoration must not modify the caller's parsed namespace.
    args = SimpleNamespace(**vars(args))
    with Recovery(args, repo, client, deadline) as recovery:
        try:
            result = _create_pull(client, repo, args, deadline, recovery)
            recovery.finish(result)
            return result
        except GVError as error:
            recovery.preserve(error)
            raise


def _create_pull(client, repo, args, deadline, recovery):
    from .metadata import assignee_identities, create_with_metadata, label_identities, reviewer_set
    from .pr_create_project import project_titles
    projects = project_titles(getattr(args, 'project', None))
    if projects and not client.dry_run:
        raise GVError('Project title resolution and PR membership assignment/readback are not implemented; only offline --dry-run planning is available',
                      'feature_unavailable', details={'repository': repo, 'stage': 'project_contract',
                                                      'mutation_attempted': False, 'mutation_outcome': 'none'})
    milestone = getattr(args, 'milestone', None)
    if milestone is not None:
        try:
            valid = (isinstance(milestone, str) and bool(milestone.strip())
                     and len(milestone.encode('utf-8')) <= 1024
                     and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in milestone))
        except UnicodeError:
            valid = False
        if not valid:
            raise GVError('Milestone name must be nonblank UTF-8 without controls, at most 1024 bytes', 'usage_error')
        if not client.dry_run:
            raise GVError('Milestone name-to-ID resolution and PR assignment are not implemented; only offline --dry-run planning is available',
                          'feature_unavailable', details={'repository': repo, 'stage': 'milestone_contract',
                                                          'mutation_attempted': False, 'mutation_outcome': 'none'})
    attachments = getattr(args, 'attach', None)
    if attachments and not client.dry_run:
        raise GVError('PR attachment upload/linking is not implemented; only local --dry-run planning is available',
                      'feature_unavailable', details={'repository': repo, 'stage': 'attachment_contract',
                                                      'mutation_attempted': False, 'mutation_outcome': 'none'})
    editor = getattr(args, 'editor', False)
    fill_first = getattr(args, 'fill_first', False)
    fill_verbose = getattr(args, 'fill_verbose', False)
    fill = getattr(args, 'fill', False) or fill_first or fill_verbose
    template = getattr(args, 'template', None)
    # gh bypasses template selection on direct fill submission; editor mode
    # instead replaces the filled body with the selected template seed.
    apply_template = template is not None and (editor or not fill)
    remote_template = False
    template_pin = None
    if apply_template and isinstance(template, str) and template and template != '-':
        remote_template = not os.path.lexists(template) and '/' not in template and '\\' not in template
        if remote_template:
            from .pr_create_template import catalog_name
            catalog_name(template)
    title = args.title if args.title is not None else ('' if editor or fill else None)
    validate_title(title, allow_blank=editor or (fill and args.title is None))
    try:
        if apply_template and (not isinstance(template, str) or not template or template == '-'):
            raise GVError('--template requires a local file path, not stdin', 'usage_error')
        text = (read_value(SimpleNamespace(value_file=template, max_bytes=1048576, group='pr'), deadline)
                if apply_template and not remote_template else
                read_value(SimpleNamespace(value_file=args.body_file, max_bytes=1048576, group='pr'), deadline)
                if args.body_file is not None else args.body)
    except (OSError, UnicodeError) as error:
        raise GVError('Cannot read a regular UTF-8 template/body file', 'usage_error') from error
    if remote_template and not client.dry_run:
        from .pr_create_template import resolve
        try:
            text, template_pin = resolve(client, repo, template, deadline)
        except (GVError, http.client.HTTPException) as error:
            if not isinstance(error, GVError):
                error = GVError('Template HTTP response interrupted; no PR submitted', 'network_error')
            error.details = {**(error.details or {}), 'repository': repo,
                             'stage': 'template_resolution', 'mutation_attempted': False,
                             'mutation_outcome': 'none'}
            raise error
    validate_body(text)
    # Preserve supplied text even when Git/default-base discovery fails next.
    recovery.prepare(title, text, args.head, args.base)
    head, head_owner = select_head(args.head, repo, deadline)
    bounded = Bounded(client, deadline)
    base = branch(args.base) if args.base is not None else configured_base(deadline)
    recovery.prepare(title, text, (head_owner + ':' if head_owner else '') + head, base)
    if base is None:
        if client.dry_run:
            raise GVError('Pass --base or configure branch.<current>.gh-merge-base for an offline creation dry-run', 'usage_error')
        base = identity(bounded.request('GET', f'repos/{repo}'), repo).get('default_branch')
    base = branch(base)
    cross_owner = bool(head_owner and head_owner.casefold() != repo.split('/')[0].casefold())
    if head == base and not cross_owner:
        raise GVError('Head and base must be different branches', 'usage_error')
    recovery.prepare(title, text, (head_owner + ':' if head_owner else '') + head, base)
    fill_context = None
    if fill:
        from .pr_create_fill import prepare
        mode = {'first': True} if fill_first else {'verbose': True} if fill_verbose else {}
        generated_title, generated_body, fill_context = prepare(repo, base, head, deadline, **mode)
        if args.title is None:
            title = generated_title
        if text is None:
            text = generated_body
        validate_title(title, allow_blank=editor)
        validate_body(text)
    if remote_template and client.dry_run:
        text = None
    editor_seed = {'title': title, 'body': None if remote_template and client.dry_run else text if text is not None else ''} if editor else None
    recovery.prepare(title, text, (head_owner + ':' if head_owner else '') + head, base)
    if editor and not client.dry_run:
        from .comment_editor import edit_text
        try:
            draft = edit_text(title + '\n' + (text or ''), deadline, max_bytes=2097154)
        except OSError:
            raise GVError('Cannot read editor draft; no PR submitted', 'editor_error') from None
        title, separator, text = draft.partition('\n')
        # Treat CRLF as one title separator; preserve all remaining body bytes.
        if separator and title.endswith('\r'):
            title = title[:-1]
        validate_title(title)
        if len(text.encode('utf-8')) > 1048576:
            raise GVError('Editor body must be at most 1 MiB UTF-8', 'usage_error')
    if not (editor and client.dry_run):
        if args.draft and not DRAFT_PREFIX.match(title):
            title = 'Draft: ' + title
        validate_title(title)
    payload = {'title': title, 'head': head, 'base': base, 'draft': args.draft}
    if getattr(args, 'no_maintainer_edit', False):
        payload['maintainer_can_modify'] = False
    if text is not None:
        payload['body'] = text
    recovery.prepare(title, text, (head_owner + ':' if head_owner else '') + head, base)
    if editor and client.dry_run:
        # Seeds are not final wire values: the editor may replace both fields.
        payload.pop('title', None)
        payload.pop('body', None)
    metadata = args.assignee or args.label or args.reviewer
    path = f'repos/{repo}/pulls'
    if client.dry_run:
        attachment_plan = None
        if attachments:
            from .pr_create_attachments import plan
            attachment_plan = plan(attachments, deadline)
        if metadata:
            result = create_with_metadata(args, client, repo, payload)
        else:
            result = client.request('POST', path, body=payload)
        if attachment_plan is not None:
            result = {'creation_plan': result, 'attachments': attachment_plan,
                      'creation_plan_body_is_original': True}
        # Keep the existing request/template keys for agent consumers. A preview
        # is local input validation, never evidence of permission, branch
        # existence, resolved metadata or successful remote creation.
        if time.monotonic() >= deadline:
            raise GVError('Creation preview deadline reached', 'deadline_exceeded')
        if editor:
            result = {**result, 'editor': {'launched': False, 'seed': editor_seed,
                                         'title_known': False, 'body_known': False, 'template_body_unresolved': remote_template}}
        if template is not None:
            result = {**result, 'template': {'source': 'repository_catalog' if remote_template else 'local_file',
                                             'name': template if remote_template else None,
                                             'applied': apply_template and not remote_template,
                                             'resolved': not remote_template,
                                             'body_known': not (editor or remote_template)}}
        if fill:
            result = {**result, 'fill': {**fill_context, 'remote_verified': False}}
        if milestone is not None:
            result = {**result, 'milestone': {'name': milestone, 'id': None, 'resolved': False,
                                             'assignment_supported': None,
                                             'status': 'live_unimplemented'}}
        if projects:
            result = {**result, 'projects': [{'title': title, 'id': None, 'resolved': False,
                                             'assignment_supported': None,
                                             'status': 'live_unimplemented'} for title in projects]}
        return {**result, 'dry_run': True, 'mutation_attempted': False,
                **({'recovered_input': {'restored': True, 'replay_allowed': not recovery.state['mutation_attempted'],
                                       'source_mutation_outcome': recovery.state['mutation_outcome']}}
                   if recovery.state is not None else {}),
                'mutation_outcome': 'none', 'network_requests': 0, 'git_push': False,
                'validation': {'local_inputs': True, 'remote_eligibility': False,
                               'metadata_resolved': not bool(metadata or projects or milestone is not None),
                               'final_request_known': not bool(metadata or projects or attachments or editor or remote_template or cross_owner or milestone is not None)},
                **({'head_selection': {'owner': head_owner, 'repository': repo,
                                      'owner_type_verified': False, 'source_resolved': False,
                                      'head_repo': None}} if head_owner else {})}
    attempted, number, pull_id, stage, pins = False, None, None, 'preflight', None
    author, repository, current_identity, source = None, None, None, None

    class Creation(Bounded):
        def request(self, method, target, query=None, body=None):
            nonlocal attempted, number, pull_id, stage, pins, author, repository, current_identity, source
            if method == 'POST' and target == path:
                repository = identity(bounded.request('GET', f'repos/{repo}'), repo)
                if head_owner and not cross_owner:
                    profile = user_value(bounded.request('GET', 'users/' + head_owner), head_owner)
                    owner = user_value(repository.get('owner'), head_owner)
                    if profile.get('type') != 'User' or owner.get('type') != 'User':
                        raise GVError('Qualified --head requires a user account, not an organization or unknown type',
                                      'usage_error')
                    if profile['id'] != owner['id']:
                        raise GVError('Qualified head owner identity was not confirmed', 'unexpected_response')
                if template_pin and repository['id'] != template_pin['repository_id']:
                    raise GVError('Template repository identity changed before creation', 'concurrent_change')
                author = user_value(self.request('GET', 'user'))
                if cross_owner:
                    from .pr_create_head import Resolver
                    source = Resolver(bounded).resolve(repo, repository, head_owner, author, head, base)
                    if (source['owner_id'] != author['id']
                            or head_owner.casefold() != author['login'].casefold()):
                        raise GVError('GitVerse head_repo has no documented owner field; third-party source wire mapping remains unverified',
                                      'feature_unavailable', details={'stage': 'source_wire_contract'})
                    fresh_repo = identity(bounded.request('GET', f'repos/{repo}'), repo)
                    if (fresh_repo['id'] != repository['id']
                            or Resolver(bounded).resolve(repo, fresh_repo, head_owner, author, head, base) != source):
                        raise GVError('Qualified head selection changed before creation', 'concurrent_change')
                    user_value(self.request('GET', 'user'))
                    payload['head_repo'] = source['head_repo']
                    body['head_repo'] = source['head_repo']
                if fill:
                    from .pr_create_fill import recheck, verify_remote
                    verify_remote(bounded, repo, base, head, fill_context, source=source)
                    recheck(fill_context, repo, deadline)
                recovery.before_mutation()
                stage, attempted = 'create', True
            result = super().request(method, target, query, body)
            if method == 'GET' and target == 'user':
                user = user_value(result)
                observed = (user['id'], user['login'].casefold())
                if current_identity is not None and observed != current_identity:
                    raise GVError('Current user changed before creation', 'concurrent_change')
                current_identity = observed
            if method == 'POST' and target == path:
                if isinstance(result, dict):
                    number = result.get('number') if positive_id(result.get('number')) else None
                    pull_id = result.get('id') if positive_id(result.get('id')) else None
                stage = 'validate_create'
                pins = validate_pull(result, payload, repo, repository['id'], author, source=source)
                if fill and pins[2:] != [fill_context['pins']['base'], fill_context['pins']['head']]:
                    raise GVError('Created PR differs from pinned fill history', 'concurrent_change')
            if args.reviewer and method == 'GET' and number is not None and target == path + '/' + str(number):
                if validate_pull(result, payload, repo, repository['id'], author, number, source=source) != pins:
                    raise GVError('Created PR identity changed before reviewer operation', 'concurrent_change')
            return result

    session = Creation(client, deadline)
    try:
        result = (create_with_metadata(args, session, repo, payload,
                                      web_transform=lambda web: Bounded(web, deadline))
                  if metadata else session.request('POST', path, body=payload))
        stage = 'verify_create'
        actual = session.request('GET', path + '/' + str(number))
        if validate_pull(actual, payload, repo, repository['id'], author, number, source=source) != pins:
            raise GVError('Created PR changed before readback', 'concurrent_change')
        if args.assignee and assignee_identities(actual) != assignee_identities(result):
            raise GVError('Created PR assignees changed before final readback', 'concurrent_change')
        if args.label and label_identities(actual) != label_identities(result):
            raise GVError('Created PR labels changed before final readback', 'concurrent_change')
        if args.reviewer and reviewer_set(actual) != reviewer_set(result):
            raise GVError('Created PR reviewers changed before final readback', 'concurrent_change')
        return {**actual, **({'metadata_completed_steps': result['metadata_completed_steps']} if metadata else {}),
                **({'fill': {**fill_context, 'remote_verified': True}} if fill else {}),
                'create_verification': {'readback': True, 'atomic': False}}
    except GVError as error:
        # Metadata preflight errors are known to precede creation. Its own partial
        # reviewer step details remain intact after the core POST/readback checks.
        error.details = {**(error.details or {}), 'repository': repo,
                         'pull_number': number, 'pull_id': pull_id,
                         'head': head, 'base': base, 'source': source,
                         'mutation_outcome': ('unknown' if (error.details or {}).get('mutation_outcome') == 'unknown' else ('confirmed' if pull_id else 'unknown')) if attempted else 'none', 'stage': (error.details or {}).get('stage', stage),
                         'mutation_attempted': attempted,
                         'recovery': 'Read the recorded PR number/ID or list this head/base before retrying. '
                                     'POST is never retried automatically; creation is not idempotent.'}
        raise
