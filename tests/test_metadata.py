import copy
import os
import unittest
from unittest.mock import Mock, patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.metadata import create_with_metadata, edit_with_metadata, label_catalog, names, reviewer_preflight, web_client


REPO = 'owner/repo'
LABELS = [{'id': 11, 'name': 'one', 'exclusive': False, 'is_archived': False}, {'id': 12, 'name': 'two', 'exclusive': False, 'is_archived': False}]
USERS = {'me': {'login': 'me', 'id': 1}, 'alice': {'login': 'alice', 'id': 2}, 'bob': {'login': 'bob', 'id': 3}}


def pull():
    return {'id': 88, 'number': 8, 'state': 'open', 'title': 'Title', 'body': 'Body', 'draft': False, 'merged': False,
            'head': {'ref': 'feature', 'sha': 'a'*40, 'repo': {'id': 7, 'full_name': REPO}}, 'base': {'ref': 'main', 'sha': 'b'*40, 'repo': {'id': 7, 'full_name': REPO}}, 'user': USERS['me'],
            'labels': [LABELS[0]], 'assignees': [USERS['alice']], 'requested_reviewers': [], 'requested_teams': []}


class Public:
    backend = 'public'
    dry_run = False
    def __init__(self):
        self.pull = copy.deepcopy(pull())
        self.labels = copy.deepcopy(LABELS)
        self.calls = []
        self.ignore_issue = False
        self.fail_issue = False
        self.reads = 0
        self.on_read = None
        self.rows = None
    def request(self, method, path, query=None, body=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(query), copy.deepcopy(body)))
        if path == 'repos/owner/repo': return {'id': 7, 'full_name': REPO, 'private': True, 'default_branch': 'main'}
        if path == 'user': return copy.deepcopy(USERS['me'])
        if path.startswith('users/'): return copy.deepcopy(USERS[path.split('/')[-1].lower()])
        if path == 'repos/owner/repo/labels': return copy.deepcopy(self.labels if query['page'] == 1 else [])
        if path == 'repos/owner/repo/pulls' and method == 'GET': return copy.deepcopy((self.rows or [self.pull]) if query['page'] == 1 else [])
        if path == 'repos/owner/repo/pulls' and method == 'POST':
            self.pull['body'] = body.get('body', '')
            self.pull.update({k: v for k, v in body.items() if k in ('title', 'body', 'draft')})
            self.pull['assignees'] = [USERS[login.lower()] for login in body.get('assignees', [])]
            self.pull['labels'] = [row for row in self.labels if row['id'] in body.get('labels', [])]
            return copy.deepcopy(self.pull)
        if path == 'repos/owner/repo/pulls/8' and method == 'GET':
            self.reads += 1
            if self.on_read: self.on_read(self, self.reads)
            return copy.deepcopy(self.pull)
        if path == 'repos/owner/repo/pulls/8' and method == 'PATCH':
            for key, value in body.items():
                if key == 'base': self.pull['base'] = {'ref': value}
                else: self.pull[key] = value
            return copy.deepcopy(self.pull)
        if path == 'repos/owner/repo/issues/8' and method == 'PATCH':
            if self.fail_issue: raise GVError('HTTP failure', 'http_error', 500)
            if not self.ignore_issue:
                if 'labels' in body: self.pull['labels'] = [row for row in self.labels if row['name'] in body['labels']]
                if 'assignees' in body: self.pull['assignees'] = [USERS[login.lower()] for login in body['assignees']]
            return {}
        raise AssertionError((method, path))
    def writes(self):
        return [c for c in self.calls if c[0] != 'GET']


class Web:
    backend = 'web'
    auth_source = 'cookie_file'
    def __init__(self, public): self.public, self.calls, self.fail = public, [], False
    def request(self, method, path, query=None, body=None, **kwargs):
        self.calls.append((method, path, query, copy.deepcopy(body)))
        if path == 'user': return {'id': 1, 'username': 'me'}
        if method == 'GET': return {'users': [USERS['bob'], USERS['alice']], 'teams': [{'id': 22}]}
        if self.fail: raise GVError('Forbidden', 'http_error', 403)
        key = 'requested_teams' if body['isTeam'] else 'requested_reviewers'
        rows = self.public.pull[key]
        if body['action'] == 'detach': self.public.pull[key] = [r for r in rows if r['id'] != body['reviewerId']]
        else: rows.append({'id': body['reviewerId'], 'login': 'bob'})
        return None


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True); self.env.start(); self.addCleanup(self.env.stop)
        self.c = Public()
        self.web = Web(self.c)
        self.web_patch = patch('gitverse_cli.metadata.web_client', return_value=self.web)
        self.web_patch.start(); self.addCleanup(self.web_patch.stop)
    def create(self, *flags):
        args = parser().parse_args(['pr', 'create', '-R', REPO, '--head', 'feature', '--base', 'main', '--title', 'Created', *flags])
        with patch('gitverse_cli.cli.Client', return_value=self.c): return execute(args)
    def edit(self, *flags):
        args = parser().parse_args(['pr', 'edit', '8', '-R', REPO, *flags])
        with patch('gitverse_cli.cli.Client', return_value=self.c): return execute(args)

    def test_names_repeat_commas_spaces_and_empty(self):
        self.assertEqual(names(['one,two', 'one', 'help wanted']), ['one', 'two', 'help wanted'])
        for values in ([''], ['a,,b'], ['bad\nvalue']):
            with self.assertRaises(GVError): names(values)

    def test_create_uses_label_ids_and_canonical_assignees_and_verifies(self):
        result = self.create('--assignee', '@me,ALICE', '--label', 'two,one')
        write = self.c.writes()[0]
        self.assertEqual(write[3]['labels'], [12, 11])
        self.assertEqual(write[3]['assignees'], ['me', 'alice'])
        self.assertEqual(result['metadata_completed_steps'], ['create'])
        self.assertEqual(self.c.calls[-1][0], 'GET')

    def test_create_unknown_archived_exclusive_labels_refuse_before_post(self):
        for mode in ('unknown', 'archive', 'exclusive'):
            self.c = Public()
            if mode == 'archive': self.c.labels[0]['is_archived'] = True
            if mode == 'exclusive':
                self.c.labels = [{'id': i, 'name': name, 'exclusive': True} for i, name in [(11, 'scope/one'), (12, 'scope/two')]]
            value = 'missing' if mode == 'unknown' else 'one' if mode == 'archive' else 'scope/one,scope/two'
            with self.assertRaises(GVError): self.create('--label', value)
            self.assertEqual(self.c.writes(), [])

    def test_label_catalog_repeated_invalid_or_bounded_is_error(self):
        c = Mock(); c.request.return_value = [LABELS[0]]
        with self.assertRaises(GVError) as e: label_catalog(c, REPO)
        self.assertEqual(e.exception.code, 'pagination_error')
        for rows in ({'items': []}, [{'id': True, 'name': 'a'}], [{'id': -1, 'name': 'a'}]):
            c.request.return_value = rows
            with self.assertRaises(GVError): label_catalog(c, REPO)
        c.request.side_effect = lambda *args: [{'id': args[2]['page'], 'name': str(args[2]['page'])}]
        with self.assertRaises(GVError) as e: label_catalog(c, REPO)
        self.assertEqual(e.exception.code, 'pagination_limit')

    def test_create_cookie_missing_refuses_before_any_remote_write(self):
        with patch('gitverse_cli.metadata.web_client', side_effect=GVError('Need cookies', 'web_session_required')):
            with self.assertRaises(GVError): self.create('--reviewer', 'bob', '--label', 'one')
        self.assertEqual(self.c.calls, [])

    def test_create_user_and_team_reviewers_use_web_ids(self):
        result = self.create('--reviewer', 'bob,team:22')
        patches = [c for c in self.web.calls if c[0] == 'PATCH']
        self.assertEqual([c[3] for c in patches], [{'action': 'attach', 'reviewerId': 3, 'isTeam': False}, {'action': 'attach', 'reviewerId': 22, 'isTeam': True}])
        self.assertEqual(result['metadata_completed_steps'], ['create', 'attach:user:3', 'attach:team:22'])
        self.assertEqual(self.web.calls[0][0], 'GET')
        self.assertTrue(all('/reviewer' not in c[1] for c in self.c.writes()))

    def test_self_review_or_invalid_team_refuses_creation(self):
        for value in ('@me', 'team:0', 'team:wrong', 'org//team'):
            with self.assertRaises(GVError): self.create('--reviewer', value)
        self.assertEqual(self.c.writes(), [])

    def test_expired_web_preflight_fails_before_creation(self):
        self.web.request = Mock(side_effect=GVError('Auth', 'http_error', 401))
        with self.assertRaises(GVError): self.create('--reviewer', 'bob')
        self.assertEqual(self.c.writes(), [])

    def test_create_partial_reviewer_failure_contains_created_pr(self):
        self.web.fail = True
        with self.assertRaises(GVError) as e: self.create('--reviewer', 'bob')
        self.assertEqual(e.exception.details['pull_number'], 8)
        self.assertEqual(e.exception.details['completed_steps'], ['create'])
        self.assertEqual(e.exception.details['stage'], 'attach_reviewer')
        self.assertEqual(len(self.c.writes()), 1)

        self.web.fail = False
        self.web.request = lambda method, *a, **kw: ({'id': 1, 'username': 'me'} if a[0] == 'user' else {'users': [USERS['bob']], 'teams': []}) if method == 'GET' else None
        with self.assertRaises(GVError) as e: self.create('--reviewer', 'bob')
        self.assertEqual(e.exception.code, 'metadata_not_confirmed')
        self.assertEqual(e.exception.details['completed_steps'], ['create'])
        self.assertEqual(e.exception.details['stage'], 'verify_reviewer')

    def test_create_metadata_ignored_never_reports_success(self):
        def corrupt(c, count): c.pull['labels'] = []
        self.c.on_read = corrupt
        with self.assertRaises(GVError) as e: self.create('--label', 'one')
        self.assertEqual(e.exception.code, 'metadata_not_confirmed')
        self.assertEqual(e.exception.details['pull_number'], 8)

    def test_edit_add_remove_preserves_unrelated_and_uses_issue_names(self):
        result = self.edit('--add-assignee', '@me', '--remove-assignee', 'alice', '--add-label', 'two', '--remove-label', 'one', '--title', 'Edited')
        self.assertEqual([c[1] for c in self.c.writes()], ['repos/owner/repo/pulls/8', 'repos/owner/repo/issues/8'])
        self.assertEqual(self.c.writes()[1][3], {'assignees': ['me'], 'labels': ['two']})
        self.assertEqual(result['metadata_completed_steps'], ['pull_metadata', 'issue_metadata'])
        self.c.calls.clear()
        result = self.edit('--add-label', 'one')
        self.assertEqual({l['name'] for l in result['labels']}, {'one', 'two'})

    def test_edit_repeat_add_and_absent_remove_is_noop(self):
        result = self.edit('--add-label', 'one', '--add-assignee', 'ALICE', '--remove-label', 'missing')
        self.assertFalse(result['metadata_changed'])
        self.assertEqual(self.c.writes(), [])

    def test_edit_clear_all_sets_and_repeat_empty_noop(self):
        result = self.edit('--remove-label', 'one', '--remove-assignee', 'alice')
        self.assertEqual(result['labels'], [])
        self.assertEqual(result['assignees'], [])
        self.c.calls.clear()
        self.assertFalse(self.edit('--remove-label', 'one', '--remove-assignee', 'alice')['metadata_changed'])
        self.assertEqual(self.c.writes(), [])

    def test_conflicting_flags_and_resolved_me_alias_never_write(self):
        for flags in (['--add-label', 'one', '--remove-label', 'one'], ['--add-assignee', '@me', '--remove-assignee', 'me'], ['--add-reviewer', 'bob', '--remove-reviewer', 'BOB']):
            with self.assertRaises(GVError): self.edit(*flags)
        self.assertEqual(self.c.writes(), [])

    def test_edit_concurrent_set_change_before_write_refuses(self):
        def move(c, count):
            if count == 2: c.pull['labels'] = []
        self.c.on_read = move
        with self.assertRaises(GVError) as e: self.edit('--add-label', 'two')
        self.assertEqual(e.exception.code, 'pr_changed')
        self.assertEqual(self.c.writes(), [])

    def test_set_change_after_plain_update_does_not_get_overwritten(self):
        def move(c, count):
            if count == 3: c.pull['labels'] = []
        self.c.on_read = move
        with self.assertRaises(GVError) as e: self.edit('--add-label', 'two', '--title', 'New')
        self.assertEqual(e.exception.code, 'pr_changed')
        self.assertEqual(e.exception.details['completed_steps'], ['pull_metadata'])
        self.assertEqual(len(self.c.writes()), 1)

    def test_ignored_issue_patch_is_error_with_partial_steps(self):
        self.c.ignore_issue = True
        with self.assertRaises(GVError) as e: self.edit('--add-label', 'two')
        self.assertEqual(e.exception.code, 'metadata_not_confirmed')
        self.assertEqual(e.exception.details['stage'], 'verify_issue')

    def test_http_mutation_failure_is_not_retried(self):
        self.c.fail_issue = True
        with self.assertRaises(GVError) as e: self.edit('--add-label', 'two', '--title', 'New')
        self.assertEqual(e.exception.details['completed_steps'], ['pull_metadata'])
        self.assertEqual(len([c for c in self.c.writes() if '/issues/' in c[1]]), 1)

    def test_reviewers_add_remove_preserve_other_users_and_teams(self):
        self.c.pull['requested_reviewers'] = [USERS['alice']]
        self.c.pull['requested_teams'] = [{'id': 44}]
        result = self.edit('--add-reviewer', 'bob,team:22', '--remove-reviewer', 'alice')
        self.assertEqual({r['id'] for r in result['requested_reviewers']}, {3})
        self.assertEqual({r['id'] for r in result['requested_teams']}, {22, 44})
        self.assertEqual([c[3]['action'] for c in self.web.calls if c[0] == 'PATCH'], ['detach', 'attach', 'attach'])

    def test_reviewer_already_requested_is_noop(self):
        self.c.pull['requested_reviewers'] = [USERS['bob']]
        result = self.edit('--add-reviewer', 'bob')
        self.assertFalse(result['metadata_changed'])
        self.assertEqual(self.web.calls, [])

    def test_cookie_missing_blocks_combined_edit_before_title(self):
        with patch('gitverse_cli.metadata.web_client', side_effect=GVError('Need cookies', 'web_session_required')):
            with self.assertRaises(GVError): self.edit('--add-reviewer', 'bob', '--title', 'New')
        self.assertEqual(self.c.calls, [])

    def test_dry_run_names_are_unresolved_without_auth_or_lookup(self):
        result = execute(parser().parse_args(['pr', 'create', '-R', REPO, '--head', 'feature', '--base', 'main', '--title', 'T', '--assignee', '@me', '--label', 'one', '--reviewer', 'bob', '--dry-run']))
        self.assertIn('unresolved_metadata', result)
        self.assertNotIn('labels', result['base_request_template']['body'])
        result = execute(parser().parse_args(['pr', 'edit', '8', '-R', REPO, '--add-label', 'one', '--dry-run']))
        self.assertEqual(result['changes']['label']['add'], ['one'])

    def test_bad_dto_missing_fields_and_identity_refuses(self):
        self.c.pull['assignees'] = None
        with self.assertRaises(GVError): self.edit('--add-assignee', 'bob')
        self.assertEqual(self.c.writes(), [])
        self.c = Public(); self.c.pull['number'] = 9
        with self.assertRaises(GVError): self.edit('--add-label', 'two')

    def test_web_session_helper_never_uses_pat_as_cookie(self):
        self.web_patch.stop()
        with patch('gitverse_cli.metadata.Client', return_value=Mock(auth_source=None)) as factory:
            with self.assertRaises(GVError) as e: web_client()
        self.assertEqual(e.exception.code, 'web_session_required')
        factory.assert_called_once_with('web')


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.c = Public()
        self.c.rows = [copy.deepcopy(pull()) for _ in range(3)]
        self.c.rows[1].update(number=9, user=USERS['alice'], draft=True, labels=LABELS, body='Literal Text', assignees=[USERS['me']], requested_reviewers=[USERS['bob']])
        self.c.rows[2].update(number=10, state='closed', merged=True)
    def filter(self, *flags):
        with patch('gitverse_cli.cli.Client', return_value=self.c):
            return execute(parser().parse_args(['pr', 'list', '-R', REPO, *flags]))
    def test_combined_filters_and_all_labels(self):
        result = self.filter('--author', 'ALICE', '--assignee', '@me', '--draft', '--label', 'one,two', '--reviewer', 'bob', '--search-text', 'literal text')
        self.assertEqual([p['number'] for p in result], [9])
        self.assertEqual(self.filter('--label', 'one,missing'), [])
    def test_merged_and_no_draft(self):
        self.assertEqual([p['number'] for p in self.filter('--state', 'merged')], [10])
        self.assertEqual([p['number'] for p in self.filter('--no-draft')], [8])
    def test_page_size_applies_after_filtering(self):
        self.assertEqual([p['number'] for p in self.filter('--state', 'all', '--author', '@me', '--limit', '1', '--page', '2')], [8])
    def test_literal_search_does_not_interpret_github_qualifiers(self):
        self.assertEqual(self.filter('--search-text', 'author:alice'), [])
    def test_missing_filter_metadata_is_unknown_not_no_match(self):
        cases = [('assignees', None, '--assignee', 'me'), ('labels', None, '--label', 'one'), ('draft', None, '--draft', None), ('merged', None, '--state', 'merged')]
        for key, value, flag, arg in cases:
            self.c.rows = [copy.deepcopy(pull())]; self.c.rows[0][key] = value
            if key == 'merged': self.c.rows[0]['state'] = 'closed'
            with self.assertRaises(GVError): self.filter(flag, *([arg] if arg else []))
    def test_dry_run_filters_do_not_resolve_me(self):
        result = execute(parser().parse_args(['pr', 'list', '-R', REPO, '--author', '@me', '--label', 'one,two', '--draft', '--dry-run']))
        self.assertEqual(result['filters']['author'], '@me')
        self.assertEqual(result['filters']['labels_all'], ['one', 'two'])


if __name__ == '__main__': unittest.main()
