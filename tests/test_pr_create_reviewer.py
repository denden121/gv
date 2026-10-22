import copy
import io
import os
import unittest
from urllib.error import HTTPError
from unittest.mock import Mock, patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError, NoRedirect
from gitverse_cli.pr_create_reviewers import handles, resolve
from test_metadata import Public, Web, REPO, USERS


class ReviewerCreateTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        self.public = Public()
        self.web = Web(self.public)
        self.web_factory = patch('gitverse_cli.metadata.web_client', return_value=self.web)
        self.web_factory.start(); self.addCleanup(self.web_factory.stop)

    def create(self, *flags):
        args = parser().parse_args(['pr', 'create', '-R', REPO, '--head', 'feature',
                                   '--base', 'main', '--title', 'Created', *flags])
        with patch('gitverse_cli.cli.Client', return_value=self.public):
            return execute(args)

    def wrap_web(self, hook):
        original = self.web.request
        def request(method, path, query=None, body=None, **kw):
            return hook(method, path, original(method, path, query, body, **kw), kw)
        self.web.request = request

    def test_login_csv_repeated_alias_and_metadata_combination(self):
        result = self.create('-r', 'bob,BOB', '--reviewer', 'team:22', '--draft',
                             '--assignee', '@me', '--label', 'one', '--body', 'Literal\r\n')
        self.assertTrue(result['draft'])
        self.assertEqual(result['body'], 'Literal\r\n')
        self.assertEqual([u['id'] for u in result['assignees']], [1])
        self.assertEqual([u['id'] for u in result['requested_reviewers']], [3])
        self.assertEqual([u['id'] for u in result['requested_teams']], [22])
        self.assertEqual(len(self.public.writes()), 1)
        self.assertEqual(len([c for c in self.web.calls if c[0] == 'PATCH']), 2)

    def test_org_slug_resolves_from_complete_short_page_catalog(self):
        original = self.public.request
        def request(method, path, query=None, body=None, **kw):
            if path == 'users/acme': return {'id': 99, 'login': 'acme', 'type': 'Organization'}
            if path == 'orgs/acme/teams':
                self.public.calls.append((method, path, query, body))
                return [{'id': 22, 'slug': 'reviewers', 'name': 'Reviewers'}] if query['page'] == 1 else []
            return original(method, path, query, body, **kw)
        self.public.request = request
        result = self.create('--reviewer', 'acme/reviewers,team:22')
        self.assertEqual([r['id'] for r in result['requested_teams']], [22])
        queries = [c[2] for c in self.public.calls if c[1] == 'orgs/acme/teams']
        self.assertEqual(queries, [{'page': 1, 'per_page': 100}, {'page': 2, 'per_page': 100}]*2)

    def test_team_catalog_limits_duplicates_shape_scope_and_permissions(self):
        for mode in ('bound', 'oversize', 'duplicate', 'shape', 'wrong-org', 'wrong-type', 'forbidden', 'missing'):
            with self.subTest(mode=mode):
                calls = []
                def request(method, path, query=None):
                    calls.append(path)
                    if path.startswith('users/'):
                        return {'id': 99, 'login': 'other' if mode == 'wrong-org' else 'acme',
                                'type': 'User' if mode == 'wrong-type' else 'Organization'}
                    if mode == 'forbidden': raise GVError('Forbidden', 'http_error', 403)
                    if mode == 'shape': return {'teams': []}
                    if mode == 'missing': return []
                    row = {'id': query['page'], 'slug': str(query['page']), 'name': 'Team'}
                    if mode == 'duplicate': return [{'id': 22, 'slug': 'reviewers', 'name': 'Team'}]
                    return [row]*101 if mode == 'oversize' else [row]
                with self.assertRaises(GVError): resolve(['acme/reviewers'], Mock(request=request))
                self.assertLessEqual(len(calls), 101)

    def test_unknown_or_malformed_inputs_are_offline_errors(self):
        for values in (['a,,b'], ['org//team'], ['team:0'], ['team:True'], ['copilot[bot]'],
                       ['bad\x7f'], ['bad\x85'], ['x'*206], [None], ['x'*1048577],
                       [','.join('user'+str(i) for i in range(101))]):
            with self.subTest(values=str(values)[:50]), self.assertRaises(GVError): handles(values)

    def test_offline_preview_no_auth_network_or_cookie_factory(self):
        self.web_factory.stop()
        with (patch('gitverse_cli.client.load_auth', side_effect=AssertionError('auth')),
             patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')),
             patch('gitverse_cli.metadata.web_client', side_effect=AssertionError('cookies'))):
            result = execute(parser().parse_args(['pr', 'create', '-R', REPO, '--head', 'feature',
                '--base', 'main', '--title', 'T', '--reviewer', 'bob,acme/reviewers', '--dry-run']))
        self.assertEqual(result['network_requests'], 0)
        self.assertEqual(result['unresolved_metadata']['reviewers'], ['bob', 'acme/reviewers'])
        self.assertFalse(result['validation']['metadata_resolved'])
        self.assertFalse(result['validation']['final_request_known'])

    def test_cookie_missing_or_wrong_actor_refuses_before_post(self):
        for actor in ({'id': 2, 'username': 'alice'}, {'id': True, 'username': 'me'},
                      {'id': 1, 'username': 'other'}, {'id': 1}, None):
            original = self.web.request
            self.web.request = lambda m, p, q=None, body=None, **kw: actor if p == 'user' else original(m,p,q,body,**kw)
            with self.assertRaises(GVError): self.create('--reviewer', 'bob')
            self.assertEqual(self.public.writes(), [])

    def test_candidate_eligibility_malformed_and_size_refuse_before_post(self):
        for response in ({'users': [], 'teams': []}, {'users': None, 'teams': []},
                         {'users': [{'id': True}], 'teams': []},
                         {'users': [{'id': 3}, {'id': 3}], 'teams': []},
                         {'users': [{'id': 3, 'isBot': True}], 'teams': []},
                         {'users': [{'id': i+2} for i in range(10001)], 'teams': []}):
            original = Web(self.public)
            self.web.request = lambda m, p, q=None, body=None, **kw: original.request(m,p,q,body,**kw) if p == 'user' else response
            with self.assertRaises(GVError): self.create('--reviewer', 'bob')
            self.assertEqual(self.public.writes(), [])

    def test_candidate_denial_deadline_and_response_bounds_no_write(self):
        for code, status in (('http_error', 401), ('http_error', 403), ('deadline_exceeded', None),
                             ('response_limit', None), ('incomplete_response', 200)):
            original = Web(self.public)
            def request(m,p,q=None,body=None,**kw):
                self.assertTrue(kw['require_complete'])
                self.assertEqual(kw['max_response_bytes'], 33554432)
                self.assertIsInstance(kw['deadline'], float)
                if p != 'user': raise GVError('Refused', code, status)
                return original.request(m,p,q,body,**kw)
            self.web.request = request
            with self.assertRaises(GVError): self.create('--reviewer', 'bob')
            self.assertEqual(self.public.writes(), [])

    def test_final_reviewer_disappearance_is_concurrent_error_with_ids(self):
        def change(c, count):
            if count == 4: c.pull['requested_reviewers'] = []
        self.public.on_read = change
        with self.assertRaises(GVError) as caught: self.create('--reviewer', 'bob')
        self.assertEqual(caught.exception.code, 'concurrent_change')
        self.assertEqual(caught.exception.details['pull_id'], 88)
        self.assertEqual(len(self.public.writes()), 1)

    def test_wrong_pr_scope_before_attach_never_patches(self):
        def change(c, count):
            if count == 2: c.pull['head']['repo']['id'] = 999
        self.public.on_read = change
        with self.assertRaises(GVError): self.create('--reviewer', 'bob')
        self.assertEqual([c for c in self.web.calls if c[0] == 'PATCH'], [])

    def test_concurrent_reviewer_before_attach_never_patches(self):
        def change(c, count):
            if count == 2: c.pull['requested_reviewers'] = [USERS['alice']]
        self.public.on_read = change
        with self.assertRaises(GVError) as caught: self.create('--reviewer', 'bob')
        self.assertEqual(caught.exception.code, 'concurrent_change')
        self.assertEqual([c for c in self.web.calls if c[0] == 'PATCH'], [])

    def test_already_requested_skips_attach_but_creation_is_never_noop(self):
        self.public.pull['requested_reviewers'] = [USERS['bob']]
        for _ in range(2): self.create('--reviewer', 'bob')
        self.assertEqual(len(self.public.writes()), 2)
        self.assertEqual([c for c in self.web.calls if c[0] == 'PATCH'], [])

    def test_duplicate_readback_reviewer_is_error(self):
        self.wrap_web(lambda m,p,r,kw: self.public.pull['requested_reviewers'].append(copy.deepcopy(USERS['bob'])) if m == 'PATCH' else r)
        with self.assertRaises(GVError): self.create('--reviewer', 'bob')

    def test_uncertain_attach_retains_ids_and_is_not_retried(self):
        original = self.web.request
        def request(m,p,q=None,body=None,**kw):
            result = original(m,p,q,body,**kw)
            if m == 'PATCH': raise GVError('Lost response', 'network_error')
            return result
        self.web.request = request
        with self.assertRaises(GVError) as caught: self.create('--reviewer', 'bob')
        details = caught.exception.details
        self.assertEqual((details['pull_id'], details['pull_number']), (88, 8))
        self.assertEqual(details['mutation_outcome'], 'unknown')
        self.assertEqual(details['reviewer_mutation_outcome'], 'unknown')
        self.assertEqual(details['reviewer'], {'id': 3, 'is_team': False})
        self.assertEqual(details['completed_steps'], ['create'])
        self.assertEqual(len([c for c in self.web.calls if c[0] == 'PATCH']), 1)

    def test_denied_attach_is_partial_confirmed_creation(self):
        self.web.fail = True
        with self.assertRaises(GVError) as caught: self.create('--reviewer', 'bob')
        self.assertEqual(caught.exception.details['mutation_outcome'], 'confirmed')
        self.assertEqual(caught.exception.details['reviewer_mutation_outcome'], 'none')
        self.assertEqual(caught.exception.details['completed_steps'], ['create'])

    def test_web_redirect_refused_without_pat_or_cross_origin_request(self):
        from test_pr_create import framed_response
        web = Client('web', dry_run=True)
        web.dry_run = False
        web.opener = Mock()
        web.opener.open.side_effect = [framed_response({'id': 1, 'username': 'me'}),
            HTTPError(web.base + '/repos/' + REPO + '/pulls/reviewers', 307, 'redirect',
                      {'Location': 'https://foreign.invalid/'}, io.BytesIO(b'{}'))]
        with patch.dict(os.environ, {'GITVERSE_TOKEN': 'synthetic-public-only'}), \
             patch('gitverse_cli.metadata.web_client', return_value=web):
            with self.assertRaises(GVError): self.create('--reviewer', 'bob')
        self.assertEqual(self.public.writes(), [])
        self.assertEqual(web.opener.open.call_count, 2)
        for call in web.opener.open.call_args_list:
            self.assertTrue(call.args[0].full_url.startswith(web.base + '/'))
            self.assertIsNone(call.args[0].get_header('Authorization'))
        self.assertIsNone(NoRedirect().redirect_request(None, None, 307, '', {}, 'https://foreign.invalid/'))
        public = Client('public', dry_run=True)
        public.dry_run = False; public.opener = Mock()
        with self.assertRaises(GVError): public.request('GET', 'https://foreign.invalid/')
        public.opener.open.assert_not_called()
        self.assertNotIn('Cookie', public.headers)

    def test_second_attach_unknown_preserves_first_completed_step(self):
        original = self.web.request
        def request(m,p,q=None,body=None,**kw):
            if m == 'PATCH' and body['isTeam']:
                self.web.calls.append((m,p,q,body))
                raise GVError('Uncertain server failure', 'http_error', 500)
            return original(m,p,q,body,**kw)
        self.web.request = request
        with self.assertRaises(GVError) as caught: self.create('-r', 'bob,team:22')
        details = caught.exception.details
        self.assertEqual(details['completed_steps'], ['create', 'attach:user:3'])
        self.assertEqual(details['mutation_outcome'], 'unknown')
        self.assertEqual(details['reviewer'], {'id': 22, 'is_team': True})
        self.assertEqual(len([c for c in self.web.calls if c[0] == 'PATCH']), 2)

    def test_attach_readback_failure_retains_unknown_in_recovery_file(self):
        import json
        from pathlib import Path
        def change(c, count):
            if count == 3: raise GVError('Readback failed', 'deadline_exceeded')
        self.public.on_read = change
        with self.assertRaises(GVError) as caught: self.create('--reviewer', 'bob')
        details = caught.exception.details
        self.assertEqual(details['mutation_outcome'], 'unknown')
        saved = json.loads(Path(details['recover_file']).read_text())
        self.assertEqual(saved['mutation_outcome'], 'unknown')
        self.assertEqual((saved['pull_id'], saved['pull_number']), (88, 8))
        self.assertEqual(len([c for c in self.web.calls if c[0] == 'PATCH']), 1)


if __name__ == '__main__': unittest.main()
