import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.repositories import repo_command, git, source_name


def repo(name='me/new', identifier=2, private=True, **extra):
    return {'id': identifier, 'full_name': name, 'private': private, 'default_branch': 'main', **extra}


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.out = self.root / 'clone'
        self.client = Mock(dry_run=False, auth_source=None)
    def call(self, replies, *args):
        self.client.reset_mock()
        self.client.request.side_effect = replies
        return repo_command(self.client, parser().parse_args(['repo', *args]))

    def test_personal_create_schema_and_readback(self):
        result = self.call([{'login': 'me'}, repo(), repo()], 'create', 'new', '--private', '--add-readme', '--description', 'Привет\n$HOME $(literal)', '--gitignore', 'Python')
        self.assertTrue(result['created']); self.assertTrue(result['private'])
        call = self.client.request.call_args_list[1]
        self.assertEqual(call.args, ('POST', 'user/repos'))
        self.assertEqual(call.kwargs['body'], {'name': 'new', 'private': True, 'auto_init': True, 'description': 'Привет\n$HOME $(literal)', 'gitignores': ['Python']})

    def test_explicit_self_namespace_and_org_endpoint(self):
        self.call([{'login': 'me'}, repo(), repo()], 'create', 'me/new', '--private')
        self.assertEqual(self.client.request.call_args_list[1].args[1], 'user/repos')
        result = self.call([{'login': 'me'}, {'name': 'org'}, repo('org/new', private=False), repo('org/new', private=False)], 'create', 'org/new', '--public')
        self.assertFalse(result['private'])
        self.assertEqual(self.client.request.call_args_list[2].args, ('POST', 'orgs/org/repos'))

    def test_visibility_or_identity_mismatch_preserves_partial_state(self):
        with self.assertRaises(GVError) as error:
            self.call([{'login': 'me'}, repo(), repo(private=False)], 'create', 'new', '--private')
        self.assertEqual(error.exception.details['stage'], 'verify_create')
        self.assertEqual(error.exception.details['completed_steps'], ['create'])

    def test_create_post_error_no_retry(self):
        with self.assertRaises(GVError) as error:
            self.call([{'login': 'me'}, GVError('network', 'network_error')], 'create', 'new', '--private')
        self.assertTrue(error.exception.details['mutation_attempted'])
        self.assertEqual(sum(c.args[0] == 'POST' for c in self.client.request.call_args_list), 1)

    def test_local_destination_is_checked_before_remote_mutation(self):
        self.out.mkdir(); (self.out / 'keep').write_text('safe')
        with self.assertRaises(GVError): self.call([], 'create', 'new', '--private', '--clone', '--dir', str(self.out))
        self.client.request.assert_not_called()
        self.assertEqual((self.out / 'keep').read_text(), 'safe')

    def test_create_clone_failure_reports_existing_repo(self):
        with patch('gitverse_cli.repositories.clone', side_effect=GVError('clone failed', 'git_error')):
            with self.assertRaises(GVError) as error:
                self.call([{'login': 'me'}, repo(), repo()], 'create', 'new', '--private', '--clone', '--dir', str(self.out))
        self.assertEqual(error.exception.details['repository'], 'me/new')
        self.assertEqual(error.exception.details['completed_steps'], ['create', 'verify_create'])
        self.assertEqual(error.exception.details['stage'], 'clone')

    def test_fork_parent_privacy_target_and_body(self):
        original = repo('source/base', 1)
        fork = repo('me/fork', fork=True, parent={'id': 1, 'full_name': 'source/base'})
        result = self.call([{'login': 'me'}, original, fork, fork], 'fork', 'source/base', '--fork-name', 'fork', '--description', 'Test')
        self.assertTrue(result['parent_verified'])
        self.assertEqual(self.client.request.call_args_list[2].kwargs['body'], {'name': 'fork', 'description': 'Test'})

    def test_async_fork_get_404_retries_only_read(self):
        original = repo('source/base', 1)
        fork = repo('org/fork', fork=True, parent={'id': 1, 'full_name': 'source/base'})
        with patch('gitverse_cli.repositories.time.sleep'):
            result = self.call([original, fork, GVError('waiting', 'http_error', 404), fork], 'fork', 'source/base', '--org', 'org', '--fork-name', 'fork')
        self.assertEqual(result['repository'], 'org/fork')
        self.assertEqual(sum(c.args[0] == 'POST' for c in self.client.request.call_args_list), 1)

    def test_fork_clone_waits_for_copied_branch_before_git(self):
        original = repo('source/base', 1)
        fork = repo('me/fork', fork=True, parent={'id': 1, 'full_name': 'source/base'})
        head = {'commit': {'sha': 'a' * 40}}
        with patch('gitverse_cli.repositories.time.sleep'), patch('gitverse_cli.repositories.clone', return_value={'empty': False}) as clone:
            result = self.call([{'login': 'me'}, original, head, fork, fork, GVError('copying', 'http_error', 404), head],
                               'fork', 'source/base', '--fork-name', 'fork', '--clone', '--dir', str(self.out))
        self.assertTrue(clone.call_args.kwargs['require_head'])
        self.assertFalse(result['clone']['empty'])

    def test_malformed_repo_metadata_is_json_error_not_attribute_error(self):
        for value in (None, {}, repo(full_name=None), repo(identifier=True)):
            with self.assertRaises(GVError): self.call([{'login': 'me'}, value], 'create', 'new', '--private')

    def test_wrong_fork_parent_visibility_or_not_fork_refuse_success(self):
        original = repo('source/base', 1)
        for fork in (repo('me/fork', fork=False), repo('me/fork', fork=True, parent={'id': 8, 'full_name': 'source/base'}),
                     repo('me/fork', private=False, fork=True, parent={'id': 1, 'full_name': 'source/base'})):
            with self.assertRaises(GVError) as error:
                self.call([{'login': 'me'}, original, fork, fork], 'fork', 'source/base', '--fork-name', 'fork')
            self.assertEqual(error.exception.details['stage'], 'verify_fork')

    def test_fork_same_target_and_post_failure_are_explicit(self):
        with self.assertRaises(GVError): self.call([{'login': 'me'}], 'fork', 'me/new')
        with self.assertRaises(GVError) as error:
            self.call([{'login': 'me'}, repo('source/base', 1), GVError('network', 'network_error')], 'fork', 'source/base')
        self.assertEqual(error.exception.details['stage'], 'fork')

    def test_input_validation_visibility_flags_and_no_clone_dir(self):
        for argv in (['create', 'new'], ['create', 'new', '--private', '--public']):
            with self.assertRaises(GVError): parser().parse_args(['repo', *argv])
        for argv in (['create', '../new', '--private'], ['create', 'new', '--private', '--dir', 'x'], ['create', 'new', '--private', '--gitignore', 'Python']):
            with self.assertRaises(GVError): self.call([], *argv)
        for value in ('https://evil.example/a/b', 'https://me:secret@gitverse.ru/a/b', 'https://gitverse.ru/a/b?token=x', '../repo', 'git@gitverse.ru:a/b'):
            with self.assertRaises(GVError): source_name(value)
        self.assertEqual(source_name('https://gitverse.ru/a/b.git'), 'a/b')

    def test_dry_run_outside_git_and_backend(self):
        for args in (['create', 'new', '--private'], ['clone', 'o/r', str(self.out)], ['fork', 'o/r', '--fork-name', 'copy']):
            value = execute(parser().parse_args(['repo', *args, '--dry-run']))
            self.assertIn('plan', value)
        self.assertFalse(self.out.exists())
        with self.assertRaises(GVError): execute(parser().parse_args(['repo', 'clone', 'o/r', '--backend', 'web', '--dry-run']))
        for args in (['create', 'new', '--private', '-R', 'o/r'], ['clone', 'o/r', '-R', 'o/r'], ['fork', 'o/r', '-R', 'other/r']):
            with self.assertRaises(GVError): execute(parser().parse_args(['repo', *args, '--dry-run']))


class CloneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root / 'source'; self.source.mkdir(); self.out = self.root / 'result'
        self.command('init', '-q', '-b', 'main')
        self.command('config', 'user.name', 'Test'); self.command('config', 'user.email', 'test@example.invalid')
        (self.source / 'hello.txt').write_text('Привет\n')
        self.command('add', '.'); self.command('commit', '-qm', 'fixture'); self.sha = self.command('rev-parse', 'HEAD')
        self.command('tag', 'v1')
        self.client = Mock(dry_run=False, auth_source=None); self.client.request.return_value = repo('owner/source')
        self.clone_calls = []
        self.addCleanup(patch.stopall)
        patch('gitverse_cli.repositories.credentials', return_value=([], dict(os.environ))).start()
        def local_git(args, cwd, env, deadline):
            if 'clone' in args:
                self.clone_calls.append((list(args), dict(env)))
                args = list(args); args[args.index('--') + 1] = self.source.as_uri()
                args = ['protocol.file.allow=always' if a == 'protocol.allow=never' else a for a in args]
            elif args[-3:] == ['remote', 'get-url', 'origin']:
                return 'https://gitverse.ru/owner/source.git'
            return git(args, cwd, env, deadline)
        patch('gitverse_cli.repositories.git', side_effect=local_git).start()
    def command(self, *args):
        return subprocess.run(['git', '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', *args], cwd=self.source, check=True, capture_output=True, text=True).stdout.strip()
    def clone(self, *flags):
        return repo_command(self.client, parser().parse_args(['repo', 'clone', 'owner/source', str(self.out), *flags]))
    def clean(self):
        self.assertFalse(self.out.exists()); self.assertFalse(list(self.root.glob('.gv-clone-*')))
    def test_clone_actual_git_contents_and_no_submodules_hooks(self):
        result = self.clone()
        self.assertEqual(result['head_sha'], self.sha)
        self.assertEqual((self.out / 'hello.txt').read_text(), 'Привет\n')
        args, env = self.clone_calls[0]
        self.assertIn('--no-recurse-submodules', args); self.assertIn('core.hooksPath=/dev/null', args)
        self.assertIn('protocol.allow=never', args); self.assertEqual(env['GIT_LFS_SKIP_SMUDGE'], '1')
        self.assertNotIn('credential', (self.out / '.git/config').read_text())
    def test_shallow_branch_and_tag_clone(self):
        self.assertEqual(self.clone('--depth', '1', '--single-branch', '--branch', 'v1')['head_sha'], self.sha)
        self.assertTrue((self.out / '.git/shallow').exists())
    def test_empty_repo_is_reported_as_empty(self):
        empty = self.root / 'empty'; empty.mkdir()
        subprocess.run(['git', 'init', '-q', str(empty)], check=True)
        self.source = empty
        result = self.clone()
        self.assertTrue(result['empty']); self.assertIsNone(result['head_sha'])
    def test_missing_branch_cleans_staging(self):
        with self.assertRaises(GVError): self.clone('--branch', 'missing')
        self.clean()
    def test_existing_path_refuses_before_api(self):
        self.out.mkdir(); (self.out / 'keep').write_text('safe')
        with self.assertRaises(GVError): self.clone()
        self.client.request.assert_not_called()
        self.assertEqual((self.out / 'keep').read_text(), 'safe')
    def test_changed_repository_id_refuses_publish(self):
        self.client.request.side_effect = [repo('owner/source'), repo('owner/source', 99)]
        with self.assertRaises(GVError): self.clone()
        self.clean()
    def test_timeout_cleans_staging(self):
        with patch('gitverse_cli.repositories.subprocess.run', side_effect=subprocess.TimeoutExpired('git', 1)):
            with self.assertRaises(GVError): self.clone()
        self.clean()

    def test_ambient_git_paths_and_config_are_removed(self):
        environment = dict(os.environ)
        environment.update(GIT_DIR='/unrelated', GIT_WORK_TREE='/unrelated', GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='x.y', GIT_CONFIG_VALUE_0='secret')
        with patch('gitverse_cli.repositories.credentials', return_value=([], environment)):
            self.clone()
        env = self.clone_calls[0][1]
        self.assertFalse(any(k in env for k in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_VALUE_0')))
