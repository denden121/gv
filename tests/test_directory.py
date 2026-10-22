import copy
import unittest
from unittest.mock import Mock, patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from gitverse_cli.directory import directory_command

ORG = {'id': 1, 'login': 'Acme', 'type': 'Organization'}
TEAM = {'id': 2, 'name': 'Reviewers', 'slug': 'reviewers'}
USER = {'id': 3, 'login': 'alice', 'type': 'User'}
REPO = {'id': 4, 'full_name': 'Acme/demo'}


class Fake:
    dry_run = False
    backend = 'public'

    def __init__(self):
        self.calls = []
        self.profile = ORG
        self.teams = [[TEAM], []]
        self.members = [[USER], []]
        self.repos = [[REPO], []]
        self.membership = None

    def request(self, method, path, query=None, **kwargs):
        self.calls.append((method, path, query, kwargs))
        if path == 'users/Acme': return copy.deepcopy(self.profile)
        if path in ('users/alice', 'user'): return copy.deepcopy(USER)
        if path.endswith('/members/alice'):
            if isinstance(self.membership, Exception): raise self.membership
            return self.membership
        pages = self.teams if path.endswith('/teams') else self.members if path.endswith('/members') else self.repos
        return copy.deepcopy(pages[query['page'] - 1])


class DirectoryTests(unittest.TestCase):
    def setUp(self): self.client = Fake()

    def call(self, *argv):
        return directory_command(self.client, parser().parse_args(argv))

    def test_org_view_identity_and_type(self):
        self.assertEqual(self.call('org', 'view', 'Acme')['organization'], ORG)
        for profile in ({**ORG, 'type': 'User'}, {**ORG, 'login': 'other'}, {**ORG, 'id': True}):
            self.client.profile = profile
            with self.assertRaises(GVError): self.call('org', 'view', 'Acme')

    def test_org_repos_local_slice_and_coverage(self):
        result = self.call('org', 'repos', 'Acme', '--limit', '1', '--page', '2')
        self.assertEqual(result['repositories'], [])
        self.assertEqual(result['total_matches'], 1)
        self.assertTrue(result['coverage']['catalog_complete'])
        self.assertEqual([c[2]['page'] for c in self.client.calls[1:]], [1, 2])

    def test_list_and_view_exact_slug_id(self):
        self.assertEqual(self.call('team', 'list', '--org', 'Acme')['teams'], [TEAM])
        self.assertEqual(self.call('team', 'view', 'Acme/reviewers')['team']['id'], 2)
        for selector in ('Acme/Reviewers', 'Acme/2', 'Acme/missing'):
            with self.assertRaises(GVError) as cm: self.call('team', 'view', selector)
            self.assertEqual(cm.exception.code, 'team_not_found')

    def test_members_role_query_child_coverage(self):
        result = self.call('team', 'members', 'Acme/reviewers', '--role', 'maintainer', '--paginate')
        self.assertEqual(result['members'], [USER])
        self.assertTrue(result['coverage']['includes_child_team_members'])
        self.assertEqual(self.client.calls[-1][2]['role'], 'maintainer')
        self.assertEqual(self.client.calls[-1][1], 'orgs/Acme/teams/reviewers/members')

    def test_team_repositories_not_org_repositories(self):
        result = self.call('team', 'repos', 'Acme/reviewers')
        self.assertEqual(result['repositories'], [REPO])
        self.assertEqual(self.client.calls[-1][1], 'orgs/Acme/teams/reviewers/repos')

    def test_membership_positive_requires_exact_status(self):
        result = self.call('org', 'member', 'Acme', '@me')
        self.assertTrue(result['is_member'])
        self.assertEqual(result['username'], 'alice')
        self.assertEqual(self.client.calls[-1][3]['expected_status'], 204)

    def test_membership_documented_negative(self):
        self.client.membership = GVError('redirect', 'http_error', 302)
        self.assertFalse(self.call('org', 'member', 'Acme', 'alice')['is_member'])

    def test_membership_inaccessible_is_never_false(self):
        for status in (301, 303, 307, 401, 403, 404, 500):
            self.client.membership = GVError('unknown', 'http_error', status)
            with self.assertRaises(GVError) as cm: self.call('org', 'member', 'Acme', 'alice')
            self.assertEqual(cm.exception.status, status)
        self.client.membership = {'member': True}
        with self.assertRaises(GVError): self.call('org', 'member', 'Acme', 'alice')

    def test_duplicate_pages_and_identities_refuse_partial_output(self):
        for batches in ([[TEAM], [TEAM]], [[TEAM, {**TEAM, 'id': 8}], []], [[TEAM, {**TEAM, 'slug': 'other'}], []]):
            self.client.teams = batches
            with self.assertRaises(GVError) as cm: self.call('team', 'list', '--org', 'Acme')
            self.assertEqual(cm.exception.code, 'pagination_error')

    def test_short_page_does_not_end_catalog(self):
        self.client.teams = [[TEAM], [{**TEAM, 'id': 9, 'slug': 'another'}], []]
        self.assertEqual(self.call('team', 'list', '--org', 'Acme', '--paginate')['total_matches'], 2)

    def test_page_bound_refuses_partial_even_view(self):
        with self.assertRaises(GVError) as cm: self.call('team', 'view', 'Acme/reviewers', '--max-pages', '1')
        self.assertEqual(cm.exception.code, 'pagination_limit')

    def test_malformed_catalogs(self):
        for batch in ({'teams': []}, [{'id': 2}], [{**TEAM, 'id': False}], [None]):
            self.client.teams = [batch, []]
            with self.assertRaises(GVError): self.call('team', 'list', '--org', 'Acme')

    def test_invalid_inputs_before_network(self):
        for argv in (('org', 'view', '../x'), ('org', 'view', 'Acme', '-R', 'o/r'), ('org', 'member', 'Acme', 'a/b'),
                     ('team', 'view', 'Acme'), ('team', 'view', 'Acme/..'), ('team', 'view', 'Acme/%2f'),
                     ('team', 'view', 'Acme/a?x'), ('team', 'view', 'Acme/a b'), ('team', 'view', 'Acme/a/b')):
            with self.assertRaises(GVError): self.call(*argv)
        self.assertEqual(self.client.calls, [])

    def test_dry_run_has_no_network_or_resolved_ids(self):
        self.client.dry_run = True
        result = self.call('team', 'members', 'Acme/reviewers', '--dry-run')
        self.assertTrue(result['dry_run']); self.assertFalse(result['writes'])
        self.assertEqual(self.client.calls, [])

    def test_execute_requires_public_and_passes_deadline(self):
        with patch('gitverse_cli.cli.Client', return_value=self.client):
            self.assertEqual(execute(parser().parse_args(['org', 'view', 'Acme']))['organization'], ORG)
            self.client.backend = 'web'
            with self.assertRaises(GVError): execute(parser().parse_args(['org', 'view', 'Acme', '--backend', 'web']))
        self.assertTrue(self.client.calls[0][3]['require_complete'])
        self.assertGreater(self.client.calls[0][3]['deadline'], 0)

    def test_transport_expected_status_rejects_empty_200(self):
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.dict('os.environ', {'GV_SETTINGS': '/private/tmp/gv-directory-no-settings.json'}, clear=True):
            client = Client()
        client.opener = Mock()
        client.opener.open.return_value = response
        with self.assertRaises(GVError) as cm: client.request('GET', 'orgs/Acme/members/alice', expected_status=204)
        self.assertEqual(cm.exception.status, 200)
        response.read.assert_not_called()

    def test_transport_accepts_empty_204(self):
        response = Mock(status=204)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b''
        with patch.dict('os.environ', {'GV_SETTINGS': '/private/tmp/gv-directory-no-settings.json'}, clear=True): client = Client()
        client.opener = Mock()
        client.opener.open.return_value = response
        self.assertIsNone(client.request('GET', 'orgs/Acme/members/alice', expected_status=204))
