import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, main, parser, repository
from gitverse_cli.client import Client, GVError
from gitverse_cli.settings import expand_aliases, load_settings, settings_path


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.path = self.root / 'preferences/settings.json'
        self.auth = self.root / 'auth.json'; self.auth.write_bytes(b'auth sentinel, never read by local commands'); self.auth.chmod(0o600)
        env = patch.dict(os.environ, {'GV_SETTINGS': str(self.path), 'GV_CONFIG': str(self.auth)}, clear=True)
        env.start(); self.addCleanup(env.stop)
    def call(self, *args): return execute(parser().parse_args(list(args)))
    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output): main(list(args))
        return json.loads(output.getvalue())
    def save_alias(self, name, argv):
        file = self.root / (name + '.json'); file.write_text(json.dumps(argv))
        return self.call('alias', 'set', name, '--argv-file', str(file))
    @contextlib.contextmanager
    def cwd(self, directory):
        before = Path.cwd(); os.chdir(directory)
        try: yield
        finally: os.chdir(before)

    def test_local_defaults_without_auth_network_or_writes(self):
        with patch('gitverse_cli.cli.Client') as client:
            result = self.call('config', 'list')
            self.assertEqual(result['config']['http_timeout']['value'], 30)
            self.assertIsNone(result['config']['default_repo']['value'])
            self.assertEqual(self.call('alias', 'list')['aliases'], {})
            client.assert_not_called()
        self.assertFalse(self.path.parent.exists())
        self.assertTrue(self.call('config', 'path')['auth_file_separate'])

    def test_config_set_get_unset_preserves_auth_and_other_sections(self):
        auth = self.auth.read_bytes()
        self.call('config', 'set', 'http_timeout', '12.5')
        self.call('config', 'set', 'default_repo', 'owner/repo')
        self.save_alias('pv', ['pr', 'view'])
        self.assertEqual(self.call('config', 'get', 'http_timeout')['value'], 12.5)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.call('config', 'set', 'http_timeout', '12.5')['changed'])
        self.assertTrue(self.call('config', 'unset', 'http_timeout')['changed'])
        self.assertFalse(self.call('config', 'unset', 'http_timeout')['changed'])
        self.assertIn('pv', self.call('alias', 'list')['aliases'])
        self.assertEqual(self.auth.read_bytes(), auth)

    def test_config_validation_does_not_store_credentials_or_unknown_keys(self):
        for key, value in (('token', 'PRIVATE'), ('api_url', 'https://evil.test'), ('default_repo', '../repo'), ('default_repo', 'https://gitverse.ru/o/r'), ('http_timeout', 'nan'), ('http_timeout', '-1')):
            with self.assertRaises(GVError): self.call('config', 'set', key, value)
        self.assertFalse(self.path.exists())

    def test_effective_environment_vs_stored_and_client_explicit_timeout(self):
        self.call('config', 'set', 'http_timeout', '12')
        self.call('config', 'set', 'default_repo', 'saved/repo')
        with patch.dict(os.environ, {'GV_HTTP_TIMEOUT': '7', 'GV_REPO': 'https://gitverse.ru/env/repo.git'}):
            self.assertEqual(self.call('config', 'get', 'http_timeout')['value'], 12)
            self.assertEqual(self.call('config', 'get', 'http_timeout', '--effective')['source'], 'GV_HTTP_TIMEOUT')
            self.assertEqual(self.call('config', 'get', 'default_repo', '--effective')['value'], 'env/repo')
            self.assertEqual(Client(dry_run=True).timeout, 7)
            self.assertEqual(Client(dry_run=True, timeout=2).timeout, 2)

    def test_default_repo_only_outside_git_and_explicit_priorities(self):
        self.call('config', 'set', 'default_repo', 'saved/repo')
        args = parser().parse_args(['repo', 'view'])
        with self.cwd(self.root), patch('gitverse_cli.cli.git', side_effect=GVError('not a repo', 'git_error')):
            self.assertEqual(repository(args), 'saved/repo')
            with patch.dict(os.environ, {'GV_REPO': 'env/repo'}): self.assertEqual(repository(args), 'env/repo')
            explicit = parser().parse_args(['repo', 'view', '-R', 'explicit/repo'])
            self.assertEqual(repository(explicit), 'explicit/repo')
            for value in ('/invalid/git/context', ''):
                with patch.dict(os.environ, {'GIT_DIR': value}):
                    with self.assertRaises(GVError): repository(args)
            (self.root / '.git').mkdir()
            with self.assertRaises(GVError): repository(args)

    def test_git_remote_wins_and_missing_or_ambiguous_remotes_do_not_fallback(self):
        self.call('config', 'set', 'default_repo', 'saved/repo')
        args = parser().parse_args(['repo', 'view'])
        with patch('gitverse_cli.cli.git', side_effect=['verse', 'https://gitverse.ru/current/repo.git']):
            self.assertEqual(repository(args), 'current/repo')
        with patch('gitverse_cli.cli.git', return_value=''):
            with self.assertRaises(GVError): repository(args)
        with patch('gitverse_cli.cli.git', side_effect=['a\nb', 'https://gitverse.ru/a/r.git', 'https://gitverse.ru/b/r.git']):
            with self.assertRaises(GVError) as error: repository(args)
            self.assertEqual(error.exception.code, 'ambiguous_repo')

    def test_settings_do_not_follow_auth_override_path(self):
        with patch.dict(os.environ, {'GV_SETTINGS': '', 'XDG_CONFIG_HOME': str(self.root / 'xdg')}):
            self.assertEqual(settings_path(), self.root / 'xdg/gitverse-agent-cli/settings.json')
        self.assertNotEqual(settings_path(), self.auth)
        with patch.dict(os.environ, {'GV_SETTINGS': str(self.auth)}):
            with self.assertRaises(GVError) as error: self.call('config', 'set', 'http_timeout', '20')
            self.assertEqual(error.exception.code, 'settings_auth_collision')

    def test_dry_run_mutations_leave_no_directories_or_lock(self):
        self.call('config', 'set', 'default_repo', 'o/r', '--dry-run')
        self.call('alias', 'set', 'pv', 'pr view $1', '--dry-run')
        self.assertFalse(self.path.parent.exists())

    def test_alias_placeholders_append_extra_args_and_preserve_global_flags(self):
        self.call('alias', 'set', 'pv', 'pr view $1')
        self.assertEqual(expand_aliases(['-R', 'o/r', 'pv', '10', '--dry-run']), ['-R', 'o/r', 'pr', 'view', '10', '--dry-run'])
        self.assertEqual(expand_aliases(['--repo=o/r', 'pv', '10']), ['--repo=o/r', 'pr', 'view', '10'])
        self.assertEqual(expand_aliases(['-Ro/r', 'pv', '10']), ['-Ro/r', 'pr', 'view', '10'])
        with self.assertRaises(GVError): expand_aliases(['pv'])

    def test_alias_all_args_and_no_placeholders(self):
        self.call('alias', 'set', 'pv', 'pr view $@')
        self.assertEqual(expand_aliases(['pv', '10', '-R', 'o/r']), ['pr', 'view', '10', '-R', 'o/r'])
        self.call('alias', 'set', 'pl', 'pr list --author @me')
        self.assertEqual(expand_aliases(['pl', '--paginate']), ['pr', 'list', '--author', '@me', '--paginate'])

    def test_json_alias_chains_and_roundtrip_import(self):
        file = self.root / 'aliases.json'; file.write_text(json.dumps({'first': ['second', '$1'], 'second': ['pr', 'view', '$1']}))
        self.call('alias', 'import', str(file))
        self.assertEqual(expand_aliases(['first', '1', '--dry-run']), ['pr', 'view', '1', '--dry-run'])
        export = self.call('alias', 'list'); file.write_text(json.dumps(export))
        self.call('alias', 'delete', '--all')
        self.call('alias', 'import', str(file))
        self.assertEqual(self.call('alias', 'list')['aliases'], export['aliases'])

    def test_alias_identical_noop_clobber_and_import_atomic_conflicts(self):
        self.call('alias', 'set', 'pv', 'pr view')
        self.assertFalse(self.call('alias', 'set', 'pv', 'pr view')['changed'])
        before = self.path.read_bytes()
        with self.assertRaises(GVError): self.call('alias', 'set', 'pv', 'pr list')
        file = self.root / 'aliases.json'; file.write_text(json.dumps({'new': ['pr', 'list'], 'pv': ['pr', 'list']}))
        with self.assertRaises(GVError): self.call('alias', 'import', str(file))
        self.assertEqual(self.path.read_bytes(), before)
        self.call('alias', 'import', str(file), '--clobber')
        self.assertEqual(self.call('alias', 'list')['aliases']['pv'], ['pr', 'list'])

    def test_cycles_builtin_shadow_external_commands_and_bad_placeholders_refused(self):
        for name, expansion in (('pr', 'pr list'), ('Bad', 'pr list'), ('rm', 'rm /tmp/file'), ('cycle', 'cycle'), ('bad', 'pr view --title=$1'), ('bad', 'pr view $0'), ('bad', 'pr view $1 $@'), ('bad', '!echo hello'), ('bad', 'pr view "')):
            with self.assertRaises(GVError): self.call('alias', 'set', name, expansion)
        self.assertFalse(self.path.exists())
        file = self.root / 'cycles.json'; file.write_text(json.dumps({'one': ['two'], 'two': ['one']}))
        with self.assertRaises(GVError): self.call('alias', 'import', str(file))

    def test_delete_dependencies_all_and_absent_noop(self):
        self.call('alias', 'set', 'pv', 'pr view')
        self.call('alias', 'set', 'other', 'pv')
        with self.assertRaises(GVError) as error: self.call('alias', 'delete', 'pv')
        self.assertEqual(error.exception.code, 'alias_in_use')
        self.call('alias', 'delete', 'other'); self.call('alias', 'delete', 'pv')
        self.assertFalse(self.call('alias', 'delete', 'missing')['changed'])
        self.call('alias', 'set', 'pv', 'pr view'); self.call('alias', 'delete', '--all')
        self.assertEqual(self.call('alias', 'list')['aliases'], {})

    def test_alias_data_never_runs_shell_or_expands_environment(self):
        target = self.root / 'must-not-exist'
        literal = '$(touch ' + str(target) + ') $HOME; `echo bad` *'
        self.save_alias('make', ['pr', 'create', '--head', 'feature', '--base', 'main', '--title', '$1'])
        result = self.invoke('make', literal, '-R', 'o/r', '--dry-run')
        self.assertEqual(result['body']['title'], literal); self.assertFalse(target.exists())

    def test_alias_expands_before_api_shorthand_and_secret_errors_stay_redacted(self):
        self.save_alias('api-user', ['api', 'user'])
        self.assertTrue(self.invoke('api-user', '--dry-run')['url'].endswith('/user'))
        self.save_alias('secret-write', ['secret', 'set', 'DEMO'])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit): main(['secret-write', '--body', 'DO_NOT_ECHO_VALUE'])
        self.assertNotIn('DO_NOT_ECHO_VALUE', stderr.getvalue())

    def test_alias_argv_and_depth_bounds(self):
        self.save_alias('pv', ['pr', 'view', '$@'])
        with self.assertRaises(GVError): expand_aliases(['pv', *(['x'] * 4097)])
        file = self.root / 'deep.json'
        data = {f'a{i}': [f'a{i+1}'] for i in range(17)}; data['a17'] = ['pr', 'list']
        file.write_text(json.dumps(data))
        with self.assertRaises(GVError): self.call('alias', 'import', str(file))

    def test_duplicate_json_bad_schema_and_private_contents_not_echoed(self):
        self.path.parent.mkdir()
        for text in ('{"version":1,"version":1}', '{"private":"DO_NOT_ECHO_VALUE"}', 'not JSON DO_NOT_ECHO_VALUE'):
            self.path.write_text(text); self.path.chmod(0o600)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit): main(['config', 'list'])
            self.assertNotIn('DO_NOT_ECHO_VALUE', stderr.getvalue())
        self.assertEqual(self.call('config', 'path')['path'], str(self.path))

    def test_insecure_symlink_fifo_and_lock_path_refused(self):
        self.path.parent.mkdir(); self.path.write_text('{}'); self.path.chmod(0o644)
        with self.assertRaises(GVError): load_settings()
        self.path.unlink(); self.path.symlink_to(self.auth)
        with self.assertRaises(GVError): load_settings()
        self.path.unlink(); os.mkfifo(self.path)
        with self.assertRaises(GVError): load_settings()
        self.path.unlink()
        Path(str(self.path) + '.lock').symlink_to(self.auth)
        with self.assertRaises(GVError): self.call('config', 'set', 'http_timeout', '10')
        self.assertEqual(self.auth.read_bytes(), b'auth sentinel, never read by local commands')

    def test_lock_contention_refused_without_overwriting(self):
        import fcntl
        self.call('config', 'set', 'http_timeout', '10'); before = self.path.read_bytes()
        with open(str(self.path) + '.lock', 'r+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(GVError) as error: self.call('config', 'set', 'http_timeout', '20')
            self.assertEqual(error.exception.code, 'settings_busy')
        self.assertEqual(self.path.read_bytes(), before)
        self.call('config', 'set', 'http_timeout', '20')

    def test_external_edit_detected_before_atomic_replace(self):
        self.call('config', 'set', 'http_timeout', '10')
        import gitverse_cli.settings as settings
        original = settings.tempfile.mkstemp
        def edit(*args, **kwargs):
            result = original(*args, **kwargs)
            data = json.loads(self.path.read_text()); data['config']['default_repo'] = 'editor/repo'
            self.path.write_text(json.dumps(data)); return result
        with patch('gitverse_cli.settings.tempfile.mkstemp', side_effect=edit):
            with self.assertRaises(GVError) as error: self.call('config', 'set', 'http_timeout', '20')
        self.assertEqual(error.exception.code, 'concurrent_settings_change')
        self.assertEqual(load_settings()['config']['default_repo'], 'editor/repo')
        self.assertEqual(load_settings()['config']['http_timeout'], 10)
        self.assertFalse(list(self.path.parent.glob('.gv-settings-*')))

    def test_write_failure_preserves_existing_json(self):
        self.call('config', 'set', 'http_timeout', '10'); before = self.path.read_bytes()
        with patch('gitverse_cli.settings.os.replace', side_effect=OSError('simulated I/O failure')):
            with self.assertRaises(OSError): self.call('config', 'set', 'http_timeout', '20')
        self.assertEqual(self.path.read_bytes(), before); self.assertFalse(list(self.path.parent.glob('.gv-settings-*')))

    def test_json_import_stdin_and_preview_does_not_write(self):
        with patch('sys.stdin', io.StringIO('{"pv":["pr","view","$1"]}')):
            result = self.call('alias', 'import', '--dry-run')
        self.assertIn('pv', result['aliases']); self.assertFalse(self.path.parent.exists())

    def test_local_scope_flags_and_shell_alias_not_accepted(self):
        for args in (('config', 'list', '-R', 'o/r'), ('alias', 'list', '--backend', 'web')):
            with self.assertRaises(GVError): self.call(*args)
        with self.assertRaises(GVError): parser().parse_args(['alias', 'set', 'shell', 'echo hi', '--shell'])
