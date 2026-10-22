import copy
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.repo_lifecycle import lifecycle


def repo(**values): return {'id': 7, 'full_name': 'me/demo', 'private': True, 'archived': False, **values}


class Fake:
    backend = 'public'
    dry_run = False

    def __init__(self):
        self.row = repo(); self.calls = []; self.collision = False; self.redirect = False
        self.ignore = False; self.failure = None; self.hook = None; self.reads = 0

    def request(self, method, path, query=None, body=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(body), kwargs))
        if method == 'GET':
            if path != 'repos/' + self.row['full_name']:
                if self.collision: return repo(id=8, full_name=path.removeprefix('repos/'))
                raise GVError('not visible', 'http_error', 404)
            self.reads += 1
            if self.hook: self.hook(self)
            return copy.deepcopy(self.row)
        if self.failure: raise self.failure
        if not self.ignore:
            if 'name' in body: self.row['full_name'] = 'me/' + body['name']
            else: self.row['archived'] = body['archived']
        if self.redirect: raise GVError('redirect', 'http_error', 307)
        return copy.deepcopy(self.row)


class LifecycleTests(unittest.TestCase):
    def setUp(self): self.client = Fake()

    def call(self, action, *args):
        return lifecycle(self.client, 'me/demo', parser().parse_args(['repo', action, *args]))

    def writes(self): return [c for c in self.client.calls if c[0] != 'GET']

    def test_archive_unarchive_exact_boolean_and_readback(self):
        result = self.call('archive')
        self.assertTrue(result['changed']); self.assertTrue(result['archived'])
        self.assertEqual(self.writes()[0][2], {'archived': True})
        result = self.call('unarchive')
        self.assertTrue(result['changed']); self.assertFalse(result['archived'])
        self.assertEqual(self.writes()[1][2], {'archived': False})
        self.assertTrue(all(c[3]['require_complete'] for c in self.client.calls))

    def test_equal_archive_state_is_noop(self):
        self.assertFalse(self.call('unarchive')['changed'])
        self.assertEqual(self.writes(), [])
        self.client.row['archived'] = True
        self.assertFalse(self.call('archive')['changed'])

    def test_rename_identity_destination_and_payload(self):
        result = self.call('rename', 'next')
        self.assertEqual(result['repository'], 'me/next')
        self.assertEqual(result['previous_repository'], 'me/demo')
        self.assertEqual(result['repository_id'], 7)
        self.assertFalse(result['local_remotes_updated'])
        self.assertEqual(self.writes()[0][:3], ('PATCH', 'repos/me/demo', {'name': 'next'}))
        self.assertEqual(self.client.calls[-1][1], 'repos/me/next')

    def test_documented_307_verifies_only_computed_destination(self):
        self.client.redirect = True
        result = self.call('rename', 'next')
        self.assertEqual(result['rename_redirect_status'], 307)
        self.assertFalse(result['verification']['redirect_followed'])
        self.assertEqual(self.client.calls[-1][1], 'repos/me/next')

    def test_rename_noop_case_insensitive(self):
        self.assertFalse(self.call('rename', 'DEMO')['changed'])
        self.assertEqual(len(self.client.calls), 1)

    def test_visible_destination_collision_refuses_before_write(self):
        self.client.collision = True
        with self.assertRaises(GVError) as cm: self.call('rename', 'next')
        self.assertEqual(cm.exception.code, 'destination_exists'); self.assertEqual(self.writes(), [])

    def test_destination_access_error_is_not_absence(self):
        original = self.client.request
        def request(method, path, *args, **kwargs):
            if path == 'repos/me/next': raise GVError('denied', 'http_error', 403)
            return original(method, path, *args, **kwargs)
        self.client.request = request
        with self.assertRaises(GVError) as cm: self.call('rename', 'next')
        self.assertEqual(cm.exception.status, 403); self.assertEqual(self.writes(), [])

    def test_previous_name_redirect_is_not_followed(self):
        original = self.client.request
        def request(method, path, *args, **kwargs):
            if method == 'GET' and path == 'repos/me/next' and self.client.row['full_name'] == 'me/demo':
                raise GVError('redirect', 'http_error', redirect)
            return original(method, path, *args, **kwargs)
        for redirect in (301, 307):
            self.client = Fake(); original = self.client.request
            self.client.request = request
            self.assertTrue(self.call('rename', 'next')['changed'])

    def test_previous_name_visible_alias_to_same_identity_is_allowed(self):
        original = self.client.request
        def request(method, path, *args, **kwargs):
            if method == 'GET' and path == 'repos/me/next' and self.client.row['full_name'] == 'me/demo':
                return copy.deepcopy(self.client.row)
            return original(method, path, *args, **kwargs)
        self.client.request = request
        self.assertTrue(self.call('rename', 'next')['changed'])

    def test_archived_repository_rename_requires_unarchive(self):
        self.client.row['archived'] = True
        with self.assertRaises(GVError) as cm: self.call('rename', 'next')
        self.assertEqual(cm.exception.code, 'repo_archived'); self.assertEqual(self.writes(), [])

    def test_dry_run_no_network(self):
        self.client.dry_run = True
        for action, args in (('archive', ()), ('unarchive', ()), ('rename', ('next',))):
            self.assertTrue(self.call(action, *args)['dry_run'])
        self.assertEqual(self.client.calls, [])

    def test_invalid_rename_names_before_network(self):
        for name in ('', '../x', 'owner/name', '.', '..', 'https://x', 'a\nb'):
            with self.assertRaises(GVError): self.call('rename', name)
        self.assertEqual(self.client.calls, [])

    def test_invalid_source_identity_or_archive_state(self):
        for key, value in (('id', True), ('full_name', 'other/demo'), ('private', None), ('archived', 'false')):
            self.client = Fake(); self.client.row[key] = value
            with self.assertRaises(GVError): self.call('archive')
            self.assertEqual(self.writes(), [])

    def test_preflight_race_refuses_write(self):
        for key, value in (('id', 9), ('private', False), ('archived', True)):
            self.client = Fake()
            def change(c):
                if c.reads == 2: c.row[key] = value
            self.client.hook = change
            with self.assertRaises(GVError) as cm: self.call('archive')
            self.assertEqual(cm.exception.code, 'concurrent_change')
            self.assertFalse(cm.exception.details['mutation_attempted']); self.assertEqual(self.writes(), [])

    def test_archive_ignored_patch_and_307_are_failures(self):
        self.client.ignore = True
        with self.assertRaises(GVError) as cm: self.call('archive')
        self.assertEqual(cm.exception.code, 'verification_failed')
        self.assertEqual(cm.exception.details['stage'], 'verify')
        self.client = Fake(); self.client.redirect = True
        with self.assertRaises(GVError) as cm: self.call('archive')
        self.assertEqual(cm.exception.status, 307)
        self.assertEqual(cm.exception.details['stage'], 'write')

    def test_rename_307_does_not_mean_success_without_readback(self):
        self.client.ignore = True; self.client.redirect = True
        with self.assertRaises(GVError) as cm: self.call('rename', 'next')
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(cm.exception.details['destination'], 'me/next')
        self.assertTrue(cm.exception.details['mutation_attempted'])

    def test_no_mutation_retries_on_network_error(self):
        self.client.failure = GVError('timeout', 'network_error')
        with self.assertRaises(GVError) as cm: self.call('archive')
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(cm.exception.details['stage'], 'write')

    def test_readback_replacement_or_visibility_change_fails(self):
        for key, value in (('id', 8), ('private', False), ('archived', False)):
            self.client = Fake()
            def change(c):
                if c.reads == 3: c.row[key] = value
            self.client.hook = change
            with self.assertRaises(GVError) as cm: self.call('archive')
            self.assertEqual(cm.exception.code, 'verification_failed')

    def test_cli_dispatch_and_web_refusal(self):
        with patch('gitverse_cli.cli.Client', return_value=self.client):
            self.assertTrue(execute(parser().parse_args(['repo', 'archive', '-R', 'me/demo']))['archived'])
            self.client.backend = 'web'
            with self.assertRaises(GVError): execute(parser().parse_args(['repo', 'unarchive', '-R', 'me/demo', '--backend', 'web']))
