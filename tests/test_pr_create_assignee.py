"""Single-flag creation behavior; synthetic transports are contract evidence only."""
import copy
import unittest
from unittest.mock import patch

from gitverse_cli.client import GVError, Client
import test_pr_create as core


class AssigneeFlagTests(unittest.TestCase):
    setUp = core.CreateTests.setUp
    create = core.CreateTests.create
    intercept = core.CreateTests.intercept

    def test_explicit_repeat_comma_case_and_self_alias_single_create(self):
        with patch('gitverse_cli.metadata.web_client', side_effect=AssertionError('cookies')):
            result = self.create('--assignee', '@me,ALICE', '-a', 'me,alice,bob')
        self.assertEqual(self.c.writes()[0][3]['assignees'], ['me', 'alice', 'bob'])
        self.assertEqual({u['id'] for u in result['assignees']}, {1, 2, 3})
        self.assertEqual(len(self.c.writes()), 1)
        self.assertEqual(sum(c[1].endswith('/pulls/8') for c in self.c.calls), 2)

    def test_invalid_names_and_count_no_network(self):
        for value in ('', 'a,,b', 'org/team', '@copilot', 'bad\nname', ','.join('u'+str(i) for i in range(101))):
            with self.subTest(value=value[:20]):
                with self.assertRaises(GVError): self.create('-a', value)
                self.assertEqual(self.c.calls, [])

    def test_wrong_self_user_dto_and_lookup_id_no_write(self):
        for target, row in [('user', {'login': 'me'}), ('user', {'id': 1, 'login': '@me'}), ('user', {'id': True, 'login': 'me'}), ('users/me', {'id': 99, 'login': 'me'}), ('users/me', {'id': 1, 'login': 'other'})]:
            self.c = core.Public()
            self.intercept(lambda m,p,r,k: row if p == target else r)
            with self.assertRaises(GVError) as caught: self.create('-a', '@me')
            self.assertFalse(caught.exception.details['mutation_attempted'])
            self.assertEqual(self.c.writes(), [])

    def test_current_identity_changes_before_post_no_write(self):
        seen = 0
        def hook(m,p,r,k):
            nonlocal seen
            if p == 'user':
                seen += 1
                if seen == 2: return {'id': 3, 'login': 'bob'}
            return r
        self.intercept(hook)
        with self.assertRaises(GVError) as caught: self.create('-a', '@me')
        self.assertEqual(caught.exception.code, 'concurrent_change')
        self.assertFalse(caught.exception.details['mutation_attempted'])
        self.assertEqual(self.c.writes(), [])

    def test_malformed_lookup_and_ambiguous_ids_no_write(self):
        for row in (None, [], {'id': 0, 'login': 'alice'}, {'id': True, 'login': 'alice'}, {'id': 1, 'login': 'alice'}):
            self.c = core.Public()
            self.intercept(lambda m,p,r,k: row if p == 'users/alice' else r)
            with self.assertRaises(GVError): self.create('-a', '@me,alice')
            self.assertEqual(self.c.writes(), [])

    def test_denied_missing_unknown_assignee_never_creates(self):
        for status in (401, 403, 404):
            self.c = core.Public()
            def hook(m,p,r,k):
                if p == 'users/alice': raise GVError('denied', 'http_error', status)
                return r
            self.intercept(hook)
            with self.assertRaises(GVError) as caught: self.create('-a', 'alice')
            self.assertFalse(caught.exception.details['mutation_attempted'])
            self.assertEqual(self.c.writes(), [])

    def test_wrong_id_login_duplicate_missing_extra_and_malformed_readbacks(self):
        for rows in (None, {}, [], [{'id': 99, 'login': 'alice'}], [{'id': 2, 'login': 'bob'}], [{'id': True, 'login': 'alice'}], [{'id': 2}], [{'id': 2, 'login': 'alice'}]*2, [{'id': 2, 'login': 'alice'}, {'id': 3, 'login': 'bob'}]):
            self.c = core.Public()
            self.c.on_read = lambda c,n: c.pull.update(assignees=copy.deepcopy(rows))
            with self.assertRaises(GVError) as caught: self.create('-a', 'alice')
            self.assertTrue(caught.exception.details['mutation_attempted'])
            self.assertEqual(caught.exception.details['pull_id'], 88)
            self.assertEqual(len(self.c.writes()), 1)

    def test_wrong_pr_id_metadata_read_refused(self):
        self.c.on_read = lambda c,n: c.pull.update(id=89)
        with self.assertRaises(GVError): self.create('-a', 'alice')
        self.assertEqual(len(self.c.writes()), 1)

    def test_wrong_repository_scope_refuses_assigned_creation(self):
        self.intercept(lambda m,p,r,k: {**r, 'full_name': 'other/repo'} if p == 'repos/owner/repo' else r)
        with self.assertRaises(GVError) as caught: self.create('-a', '@me')
        self.assertFalse(caught.exception.details['mutation_attempted'])
        self.assertEqual(self.c.writes(), [])

    def test_server_assignment_permission_denial_is_not_retried(self):
        def hook(m,p,r,k):
            if m == 'POST': raise GVError('denied', 'http_error', 403)
            return r
        self.intercept(hook)
        with self.assertRaises(GVError) as caught: self.create('-a', 'alice')
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(len(self.c.writes()), 1)
        self.assertTrue(caught.exception.details['mutation_attempted'])

    def test_final_read_concurrent_assignee_change_refuses_preserves_ids(self):
        def hook(c,n):
            if n == 2: c.pull['assignees'] = []
        self.c.on_read = hook
        with self.assertRaises(GVError) as caught: self.create('-a', 'alice')
        self.assertEqual(caught.exception.code, 'concurrent_change')
        self.assertEqual(caught.exception.details['stage'], 'verify_create')
        self.assertEqual(caught.exception.details['pull_number'], 8)
        self.assertEqual(len(self.c.writes()), 1)

    def test_alias_duplicates_are_not_noop_creation(self):
        self.create('-a', '@me,me,ME')
        self.assertEqual(self.c.writes()[0][3]['assignees'], ['me'])
        self.assertEqual(len(self.c.writes()), 1)
        self.create('-a', '@me')
        self.assertEqual(len(self.c.writes()), 2)

    def test_metadata_timeout_size_incomplete_refusals_and_single_deadline(self):
        for code in ('deadline_exceeded', 'response_limit', 'incomplete_response'):
            self.c = core.Public()
            def hook(m,p,r,k):
                if p == 'users/alice': raise GVError('bounded', code)
                return r
            self.intercept(hook)
            with self.assertRaises(GVError): self.create('-a', 'alice')
            self.assertEqual(self.c.writes(), [])
        self.c = core.Public(); options=[]
        self.intercept(lambda m,p,r,k: options.append(k) or r)
        self.create('-a', '@me,alice', '--label', 'one')
        self.assertEqual(len({k['deadline'] for k in options}), 1)
        self.assertTrue(all(k['require_complete'] and k['max_response_bytes']==33554432 for k in options))

    def test_unknown_post_and_failed_readback_never_retry(self):
        for target in ('repos/owner/repo/pulls', 'repos/owner/repo/pulls/8'):
            self.c = core.Public()
            def hook(m,p,r,k):
                if p == target: raise GVError('unknown', 'network_error')
                return r
            self.intercept(hook)
            with self.assertRaises(GVError) as caught: self.create('-a', 'alice')
            self.assertEqual(len(self.c.writes()), 1)
            self.assertTrue(caught.exception.details['mutation_attempted'])
            self.assertEqual(caught.exception.details['pull_number'], 8 if target.endswith('/8') else None)

    def test_offline_dryrun_never_resolves_or_loads_cookies(self):
        self.c = Client(dry_run=True)
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')), patch('builtins.input', side_effect=AssertionError('prompt')), patch('gitverse_cli.metadata.web_client', side_effect=AssertionError('cookies')):
            result=self.create('--assignee', '@me,alice', '--dry-run')
        self.assertEqual(result['unresolved_metadata']['assignees'], ['@me', 'alice'])
        self.assertNotIn('assignees', result['base_request_template']['body'])
        self.assertNotIn('number', result)

    def test_assignee_label_pagination_limit_prevents_post(self):
        self.intercept(lambda m,p,r,k: [{'id': self.c.calls[-1][2]['page'], 'name': str(self.c.calls[-1][2]['page']), 'exclusive': False, 'is_archived': False}] if p.endswith('/labels') else r)
        with self.assertRaises(GVError) as caught: self.create('-a', 'alice', '--label', 'one')
        self.assertEqual(caught.exception.code, 'pagination_limit')
        self.assertEqual(self.c.writes(), [])
