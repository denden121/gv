import copy
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.repo_delete import delete_repository


def row(**values):
    return {'id': 7, 'full_name': 'me/demo', 'private': True, 'archived': False,
            'permissions': {'admin': True}, **values}


class Fake:
    backend = 'public'
    dry_run = False

    def __init__(self):
        self.row = row(); self.calls = []; self.reads = 0; self.deleted = False
        self.ignore = False; self.result = None; self.fail = None; self.verify_status = 404; self.hook = None

    def request(self, method, path, query=None, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == 'GET':
            if self.deleted:
                if self.verify_status: raise GVError('not available', 'http_error', self.verify_status)
                return row(id=8)
            self.reads += 1
            if self.hook: self.hook(self)
            return copy.deepcopy(self.row)
        if self.fail: raise self.fail
        if not self.ignore: self.deleted = True
        return self.result


class DeleteTests(unittest.TestCase):
    def setUp(self): self.client = Fake()

    def call(self, *argv):
        return delete_repository(self.client, 'me/demo', parser().parse_args(['repo', 'delete', *argv]))

    def writes(self): return [c for c in self.client.calls if c[0] != 'GET']

    def test_delete_exact_204_then_404(self):
        result = self.call('-R', 'me/demo', '--yes', '--match-id', '7')
        self.assertTrue(result['deleted']); self.assertTrue(result['deletion_accepted'])
        self.assertEqual(result['repository_id'], 7)
        self.assertFalse(result['local_cleanup_performed'])
        self.assertEqual([c[0] for c in self.client.calls], ['GET', 'GET', 'DELETE', 'GET'])
        self.assertEqual(self.writes()[0][2]['expected_status'], 204)
        self.assertTrue(self.writes()[0][2]['require_complete'])
        self.assertGreater(self.writes()[0][2]['deadline'], 0)

    def test_missing_confirmation_or_explicit_repo_refuses_before_client(self):
        for argv in ([], ['--yes'], ['-R', 'me/demo']):
            with patch('gitverse_cli.cli.Client') as factory:
                with self.assertRaises(GVError) as cm:
                    execute(parser().parse_args(['repo', 'delete', *argv]))
                self.assertEqual(cm.exception.code, 'usage_error')
                factory.assert_not_called()

    def test_env_repo_is_not_explicit_confirmation(self):
        with patch.dict('os.environ', {'GV_REPO': 'me/demo'}), patch('gitverse_cli.cli.Client') as factory:
            with self.assertRaises(GVError): execute(parser().parse_args(['repo', 'delete', '--yes']))
            factory.assert_not_called()

    def test_preview_needs_explicit_repo_but_not_yes_and_no_network(self):
        self.client.dry_run = True
        result = self.call('-R', 'me/demo', '--dry-run', '--match-id', '7')
        self.assertTrue(result['dry_run']); self.assertEqual(result['method'], 'DELETE')
        self.assertEqual(self.client.calls, [])

    def test_id_mismatch_before_mutation(self):
        with self.assertRaises(GVError) as cm: self.call('-R', 'me/demo', '--yes', '--match-id', '8')
        self.assertEqual(cm.exception.code, 'repository_id_mismatch')
        self.assertFalse(cm.exception.details['mutation_attempted']); self.assertEqual(self.writes(), [])

    def test_admin_permission_must_be_boolean_true(self):
        for value in (None, {}, {'admin': False}, {'admin': 1}, {'admin': 'true'}):
            self.client.row['permissions'] = value
            with self.assertRaises(GVError) as cm: self.call('-R', 'me/demo', '--yes')
            self.assertEqual(cm.exception.code, 'permission_not_confirmed')
        self.assertEqual(self.writes(), [])

    def test_invalid_identity_archive_privacy_refuses(self):
        for key, value in (('id', True), ('full_name', 'other/demo'), ('private', None), ('archived', None)):
            self.client = Fake(); self.client.row[key] = value
            with self.assertRaises(GVError): self.call('-R', 'me/demo', '--yes')
            self.assertEqual(self.writes(), [])

    def test_preflight_replacement_or_state_race_refuses(self):
        for key, value in (('id', 8), ('private', False), ('archived', True)):
            self.client = Fake()
            def change(c):
                if c.reads == 2: c.row[key] = value
            self.client.hook = change
            with self.assertRaises(GVError) as cm: self.call('-R', 'me/demo', '--yes')
            self.assertEqual(cm.exception.code, 'concurrent_change'); self.assertEqual(self.writes(), [])

    def test_missing_or_inaccessible_is_not_successful_delete_noop(self):
        def request(*args, **kwargs): raise GVError('not visible', 'http_error', 404)
        self.client.request = request
        with self.assertRaises(GVError) as cm: self.call('-R', 'me/demo', '--yes')
        self.assertEqual(cm.exception.status, 404)

    def test_redirect_never_changes_target_or_retries(self):
        self.client.fail = GVError('moved', 'http_error', 307)
        with self.assertRaises(GVError) as cm: self.call('-R', 'me/demo', '--yes')
        self.assertEqual(cm.exception.status, 307); self.assertEqual(len(self.writes()), 1)
        self.assertEqual(cm.exception.details['stage'], 'delete')

    def test_network_error_no_retry_preserves_uncertain_outcome(self):
        self.client.fail = GVError('timeout', 'network_error')
        with self.assertRaises(GVError) as cm: self.call('-R', 'me/demo', '--yes')
        self.assertTrue(cm.exception.details['mutation_attempted'])
        self.assertEqual(cm.exception.details['repository_id'], 7)
        self.assertEqual(len(self.writes()), 1)

    def test_unexpected_delete_payload_is_not_success(self):
        self.client.result = {'ok': True}
        with self.assertRaises(GVError) as cm: self.call('-R', 'me/demo', '--yes')
        self.assertEqual(cm.exception.code, 'unexpected_response')

    def test_ignored_delete_and_recreated_name_not_silent_success(self):
        for ignore in (True, False):
            self.client = Fake(); self.client.ignore = ignore; self.client.verify_status = None
            with self.assertRaises(GVError) as cm: self.call('-R', 'me/demo', '--yes')
            self.assertEqual(cm.exception.code, 'deletion_not_confirmed')
            self.assertEqual(cm.exception.details['stage'], 'verify')
            self.assertEqual(len(self.writes()), 1)

    def test_verify_other_statuses_are_not_absence(self):
        for status in (301, 401, 403, 409, 500):
            self.client = Fake(); self.client.verify_status = status
            with self.assertRaises(GVError) as cm: self.call('-R', 'me/demo', '--yes')
            self.assertEqual(cm.exception.status, status)
            self.assertEqual(cm.exception.details['stage'], 'verify')

    def test_cli_dispatch_public_only(self):
        with patch('gitverse_cli.cli.Client', return_value=self.client):
            result = execute(parser().parse_args(['repo', 'delete', '-R', 'me/demo', '--yes']))
            self.assertTrue(result['deleted'])
            self.client.backend = 'web'
            with self.assertRaises(GVError): execute(parser().parse_args(['repo', 'delete', '-R', 'me/demo', '--yes']))
