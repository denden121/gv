import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from gitverse_cli.cli import execute, parser, repository
from gitverse_cli.client import GVError
from gitverse_cli.repo_default import KEY, read_default


class DefaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.previous = Path.cwd()
        self.addCleanup(os.chdir, self.previous)
        self.env = patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith('GIT_') and k not in ('GV_REPO', 'GV_SETTINGS')}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        os.chdir(self.root)
        self.git('init', '--template=')
        self.git('remote', 'add', 'origin', 'https://gitverse.ru/o/a.git')
        self.git('remote', 'add', 'upstream', 'https://gitverse.ru/o/b.git')
        self.client = Mock()
        self.client.request.return_value = {'id': 7, 'full_name': 'o/b', 'private': True}
        self.factory = patch('gitverse_cli.repo_default.Client', return_value=self.client)
        self.factory.start(); self.addCleanup(self.factory.stop)

    def git(self, *args):
        return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout.strip()

    def call(self, *args): return execute(parser().parse_args(['repo', 'set-default', *args]))

    def resolve(self, *args): return repository(parser().parse_args(['repo', 'view', *args]))

    def test_multiple_remotes_select_persist_resolve_and_readback(self):
        with self.assertRaises(GVError) as cm: self.resolve()
        self.assertEqual(cm.exception.code, 'ambiguous_repo')
        result = self.call('o/b')
        self.assertTrue(result['changed'])
        self.assertEqual(read_default(), 'o/b'); self.assertEqual(self.resolve(), 'o/b')
        self.assertEqual(self.client.request.call_args.args[:2], ('GET', 'repos/o/b'))
        self.assertTrue(self.client.request.call_args.kwargs['require_complete'])

    def test_explicit_and_env_keep_priority(self):
        self.call('o/b')
        self.assertEqual(self.resolve('-R', 'explicit/repo'), 'explicit/repo')
        with patch.dict(os.environ, {'GV_REPO': 'env/repo'}): self.assertEqual(self.resolve(), 'env/repo')

    def test_view_and_noop_are_auth_free(self):
        self.assertIsNone(self.call('--view')['default_repository'])
        self.client.request.assert_not_called()
        self.call('o/b'); self.client.reset_mock()
        self.assertEqual(self.call('--view')['default_repository'], 'o/b')
        self.assertFalse(self.call('o/b')['changed'])
        self.client.request.assert_not_called()

    def test_unset_restores_ambiguous_selection_and_is_idempotent(self):
        self.call('o/b'); self.client.reset_mock()
        self.assertTrue(self.call('--unset')['changed'])
        self.assertIsNone(read_default())
        self.assertFalse(self.call('--unset')['changed'])
        with self.assertRaises(GVError): self.resolve()
        self.client.request.assert_not_called()

    def test_dry_run_does_not_write_or_authenticate(self):
        before = (self.root / '.git/config').read_bytes()
        self.assertTrue(self.call('o/b', '--dry-run')['dry_run'])
        self.assertEqual((self.root / '.git/config').read_bytes(), before)
        self.client.request.assert_not_called()

    def test_invalid_scope_missing_argument_and_unknown_remote(self):
        for args in ((), ('o/b', '--view'), ('--unset', '-R', 'o/a'), ('--view', '--backend', 'public'), ('o/unknown',), ('https://gitverse.ru/o/b',)):
            with self.assertRaises(GVError): self.call(*args)
        self.client.request.assert_not_called()

    def test_stale_default_refuses_inference_until_unset(self):
        self.call('o/b'); self.git('remote', 'remove', 'upstream')
        with self.assertRaises(GVError) as cm: self.resolve()
        self.assertEqual(cm.exception.code, 'stale_local_default')
        with self.assertRaises(GVError): self.call('--view')
        self.call('--unset'); self.assertEqual(self.resolve(), 'o/a')

    def test_duplicate_and_malformed_values_refuse_and_explicit_unset_repairs(self):
        for values in (['o/a', 'o/b'], ['sensitive\nvalue'], ['https://example.org/x/y']):
            for value in values: self.git('config', '--local', '--add', KEY, value)
            with self.assertRaises(GVError) as cm: self.resolve()
            self.assertEqual(cm.exception.code, 'invalid_local_default')
            self.call('--unset'); self.assertIsNone(read_default())

    def test_local_only_global_and_include_defaults_ignored(self):
        config = self.root / 'global-config'; config.write_text('[gitverse]\n defaultRepository = o/a\n')
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(config)}): self.assertIsNone(read_default())
        self.git('config', '--local', 'include.path', str(config))
        self.assertIsNone(read_default())

    def test_api_identity_failure_leaves_config_untouched(self):
        before = (self.root / '.git/config').read_bytes()
        self.client.request.return_value = {'id': 7, 'full_name': 'other/repo', 'private': True}
        with self.assertRaises(GVError): self.call('o/b')
        self.assertEqual((self.root / '.git/config').read_bytes(), before)

    def test_remote_race_during_pat_check_refuses_write(self):
        def request(*args, **kwargs):
            self.git('remote', 'set-url', 'upstream', 'https://gitverse.ru/o/c.git')
            return {'id': 7, 'full_name': 'o/b', 'private': True}
        self.client.request.side_effect = request
        with self.assertRaises(GVError) as cm: self.call('o/b')
        self.assertEqual(cm.exception.code, 'concurrent_change'); self.assertIsNone(read_default())

    def test_git_lock_failure_preserves_old_config_and_error_context(self):
        before = (self.root / '.git/config').read_bytes()
        (self.root / '.git/config.lock').write_text('existing lock')
        with self.assertRaises(GVError) as cm: self.call('o/b')
        self.assertTrue(cm.exception.details['local_write_attempted'])
        self.assertEqual((self.root / '.git/config').read_bytes(), before)

    def test_unrelated_git_config_preserved(self):
        self.git('config', '--local', 'test.keep', 'literal $HOME $(text)')
        self.call('o/b'); self.call('--unset')
        self.assertEqual(self.git('config', '--local', '--get', 'test.keep'), 'literal $HOME $(text)')

    def test_outside_git_refuses_without_using_saved_settings(self):
        os.chdir(self.root.parent)
        with self.assertRaises(GVError) as cm: self.call('--view')
        self.assertEqual(cm.exception.code, 'git_required')
        self.assertIsNone(read_default())
