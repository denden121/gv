import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

from gitverse_cli.checkout import checkout_pr, git_command, pr_reference
from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from gitverse_cli.git_credential import answer


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source, self.clone = self.root / 'source', self.root / 'clone'
        self.source.mkdir()
        self.git('init', '-q', '-b', 'main', cwd=self.source)
        self.git('config', 'user.name', 'CLI test', cwd=self.source)
        self.git('config', 'user.email', 'cli-test@example.invalid', cwd=self.source)
        (self.source / 'base.txt').write_text('base\n')
        self.git('add', '.', cwd=self.source)
        self.git('commit', '-qm', 'base', cwd=self.source)
        self.base = self.git('rev-parse', 'HEAD', cwd=self.source)
        self.git('switch', '-qc', 'feature/test', cwd=self.source)
        (self.source / 'feature.txt').write_text('feature\n')
        self.git('add', '.', cwd=self.source)
        self.git('commit', '-qm', 'feature', cwd=self.source)
        self.sha = self.git('rev-parse', 'HEAD', cwd=self.source)
        self.git('switch', '-q', 'main', cwd=self.source)
        self.git('clone', '-q', str(self.source), str(self.clone), cwd=self.root)
        self.git('remote', 'add', 'verse', 'https://gitverse.ru/owner/repo.git', cwd=self.clone)
        self.git('config', 'user.name', 'CLI test', cwd=self.clone)
        self.git('config', 'user.email', 'cli-test@example.invalid', cwd=self.clone)
        self.previous_cwd = Path.cwd()
        os.chdir(self.clone)
        self.client = Mock(backend='public', dry_run=False, auth_source='config')
        self.client.request.return_value = {'id': 50, 'number': 5, 'base': {'repo': {'full_name': 'owner/repo'}}, 'head': {'sha': self.sha, 'ref': 'feature/test', 'repo': {'full_name': 'owner/repo'}}}
        self.fetch_calls = []
        self.credentials_patch = patch('gitverse_cli.checkout.credentials', return_value=([], dict(os.environ)))
        self.credentials_patch.start()
        def local_git(*args, **kwargs):
            if args[0] == 'fetch':
                self.fetch_calls.append(args)
                args = list(args)
                args[args.index('--') + 1] = str(self.source)
            return git_command(*args, **kwargs)
        self.git_patch = patch('gitverse_cli.checkout.git_command', side_effect=local_git)
        self.git_patch.start()

    def tearDown(self):
        self.git_patch.stop()
        self.credentials_patch.stop()
        os.chdir(self.previous_cwd)
        self.temp.cleanup()

    def git(self, *args, cwd=None):
        result = subprocess.run(['git', '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', *args], cwd=cwd, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def checkout(self, *options):
        args = parser().parse_args(['pr', 'checkout', '5', '-R', 'owner/repo', *options])
        return checkout_pr(args, self.client, 'owner/repo', 5)

    def assert_head(self, sha):
        self.assertEqual(self.git('rev-parse', 'HEAD'), sha)
        self.assertEqual(self.git('for-each-ref', '--format=%(refname)', 'refs/gv/checkout/'), '')

    def test_fetches_exact_sha_creates_branch_and_cleans_ref(self):
        result = self.checkout()
        self.assertEqual(result['action'], 'created')
        self.assertEqual(result['branch'], 'feature/test')
        self.assert_head(self.sha)
        self.assertIn(self.sha + ':refs/gv/checkout/', self.fetch_calls[0][-1])
        self.assertEqual(self.git('remote', 'get-url', 'verse'), 'https://gitverse.ru/owner/repo.git')
        self.assertEqual(self.git('config', '--local', '--get-regexp', 'remote.*url'), 'remote.origin.url ' + str(self.source) + '\nremote.verse.url https://gitverse.ru/owner/repo.git')

    def test_detached_checkout_and_repeat(self):
        result = self.checkout('--detach')
        self.assertTrue(result['detached'])
        self.assertIsNone(result['branch'])
        self.assert_head(self.sha)
        self.assertTrue(self.checkout('--detach')['detached'])

    def test_existing_branch_is_only_fast_forwarded(self):
        self.git('branch', 'feature/test', self.base)
        self.assertEqual(self.checkout()['action'], 'fast_forwarded')
        self.assert_head(self.sha)
        self.assertEqual(self.checkout()['action'], 'already_current')

    def test_diverged_branch_and_local_commit_are_preserved(self):
        self.git('switch', '-qc', 'feature/test')
        (self.clone / 'local.txt').write_text('local\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'local commit')
        local = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main')
        with self.assertRaises(GVError) as error:
            self.checkout()
        self.assertEqual(error.exception.code, 'branch_diverged')
        self.assert_head(self.base)
        self.assertEqual(self.git('rev-parse', 'feature/test'), local)

    def test_dirty_or_untracked_file_stops_before_api_or_fetch(self):
        for file in ('base.txt', 'untracked.txt'):
            path = self.clone / file
            path.write_text('do not lose\n')
            with self.assertRaises(GVError) as error:
                self.checkout()
            self.assertEqual(error.exception.code, 'dirty_worktree')
            self.assertEqual(path.read_text(), 'do not lose\n')
            self.client.request.assert_not_called()
            if file == 'base.txt':
                path.write_text('base\n')
            else:
                path.unlink()
        self.assertFalse(self.fetch_calls)

    def test_ignored_file_collision_is_not_overwritten(self):
        (self.clone / '.git' / 'info' / 'exclude').write_text('feature.txt\n')
        (self.clone / 'feature.txt').write_text('ignored local content\n')
        with self.assertRaises(GVError):
            self.checkout()
        self.assert_head(self.base)
        self.assertEqual((self.clone / 'feature.txt').read_text(), 'ignored local content\n')

    def test_explicit_local_branch(self):
        self.assertEqual(self.checkout('--branch', 'review/pr-5')['branch'], 'review/pr-5')
        self.assert_head(self.sha)

    def test_named_branch_fast_forward_and_noop_preserve_source_branch(self):
        self.git('branch', 'review/pr-5', self.base)
        self.git('branch', 'feature/test', self.base)
        result = self.checkout('-b', 'review/pr-5')
        self.assertEqual(result['action'], 'fast_forwarded')
        self.assertEqual(result['previous_target_sha'], self.base)
        self.assertEqual(self.git('branch', '--show-current'), 'review/pr-5')
        self.assertEqual(self.git('rev-parse', 'feature/test'), self.base)
        self.assertEqual(self.checkout('--branch', 'review/pr-5')['action'], 'already_current')
        self.assert_head(self.sha)

    def test_named_diverged_branch_is_preserved(self):
        self.git('switch', '-qc', 'review/pr-5')
        self.git('commit', '--allow-empty', '-qm', 'local')
        local = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main')
        with self.assertRaises(GVError) as error:
            self.checkout('--branch', 'review/pr-5')
        self.assertEqual(error.exception.code, 'branch_diverged')
        self.assertFalse(error.exception.details['local_mutation_attempted'])
        self.assertEqual(self.git('rev-parse', 'review/pr-5'), local)
        self.assert_head(self.base)

    def test_named_branch_with_same_named_tag_uses_full_branch_identity(self):
        self.git('tag', 'review', self.base)
        result = self.checkout('--branch', 'review')
        self.assertEqual(result['branch'], 'review')
        self.assertEqual(self.git('symbolic-ref', 'HEAD'), 'refs/heads/review')
        self.assertEqual(self.git('rev-parse', 'refs/tags/review'), self.base)
        self.assertEqual(self.checkout('--branch', 'review')['action'], 'already_current')
        self.assert_head(self.sha)

    def test_invalid_explicit_branch_never_falls_back_or_fetches(self):
        for name in ('', 'HEAD', '-bad', 'bad..name', 'refs/heads/../bad', 'bad name', '@{-1}'):
            for dry_run in (False, True):
                with self.subTest(name=name, dry_run=dry_run):
                    self.client.dry_run = dry_run
                    with self.assertRaises(GVError) as error:
                        self.checkout('--branch=' + name)
                    self.assertEqual(error.exception.code, 'usage_error')
                    self.client.request.assert_not_called()
                    self.assertFalse(self.fetch_calls)
                    self.assert_head(self.base)

    def test_branch_and_detach_are_exclusive_before_network(self):
        with self.assertRaises(GVError):
            self.checkout('--branch', 'review', '--detach')
        self.client.request.assert_not_called()
        self.assertFalse(self.fetch_calls)

    def test_fork_with_named_branch_fetches_source_and_keeps_head_ref_local(self):
        self.client.request.return_value['head']['repo']['full_name'] = 'fork-owner/fork-repo'
        result = self.checkout('--branch', 'review/fork')
        self.assertEqual(result['source_repository'], 'fork-owner/fork-repo')
        self.assertEqual(self.git('branch', '--show-current'), 'review/fork')
        self.assertIn('https://gitverse.ru/fork-owner/fork-repo.git', self.fetch_calls[0])
        self.assertEqual(self.git('for-each-ref', '--format=%(refname)', 'refs/heads/feature/test'), '')
        self.assert_head(self.sha)

    def test_same_sha_concurrent_branch_switch_does_not_report_success(self):
        def switch_at_verify(*args, **kwargs):
            if args == ('rev-parse', 'HEAD'):
                self.git('switch', '-qc', 'concurrent')
            if args[0] == 'fetch':
                args = list(args)
                args[args.index('--') + 1] = str(self.source)
            return git_command(*args, **kwargs)
        with patch('gitverse_cli.checkout.git_command', side_effect=switch_at_verify):
            with self.assertRaises(GVError) as error:
                self.checkout('--branch', 'review/pr-5')
        self.assertEqual(error.exception.code, 'branch_changed')
        self.assertEqual(error.exception.details['stage'], 'verify')
        self.assertTrue(error.exception.details['local_mutation_attempted'])
        self.assertEqual(error.exception.details['branch'], 'review/pr-5')
        self.assertEqual(self.git('rev-parse', 'review/pr-5'), self.sha)
        self.assert_head(self.sha)

    def test_named_branch_offline_preview_uses_real_client_without_network(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('offline')):
            result = execute(parser().parse_args(['pr', 'checkout', '5', '-R', 'owner/repo',
                                                 '--branch', 'review/offline', '--dry-run']))
        self.assertEqual(result['local_checkout']['branch'], 'review/offline')
        self.assertEqual(self.git('branch', '--show-current'), 'main')
        self.assertEqual(self.git('for-each-ref', '--format=%(refname)', 'refs/heads/review/offline'), '')
        self.assertFalse(self.fetch_calls)

    def test_invalid_named_branch_with_branch_selector_refuses_offline(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('offline')):
            with self.assertRaises(GVError) as error:
                execute(parser().parse_args(['pr', 'checkout', 'feature/test', '-R', 'owner/repo',
                                             '--branch=', '--dry-run']))
        self.assertEqual(error.exception.code, 'usage_error')
        self.assertFalse(self.fetch_calls)

    def test_named_branch_refuses_partial_oversized_and_denied_responses(self):
        import urllib.error
        client = Client(dry_run=True)
        client.dry_run = False
        args = parser().parse_args(['pr', 'checkout', '5', '-R', 'owner/repo', '--branch', 'review'])
        for status, body, code in ((206, b'{}', 'incomplete_response'),
                                   (200, b' ' * 1048577, 'response_limit')):
            response = Mock(status=status)
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read1.side_effect = io.BytesIO(body).read1
            with self.subTest(status=status), patch.object(client.opener, 'open', return_value=response) as opened:
                with self.assertRaises(GVError) as error:
                    checkout_pr(args, client, 'owner/repo', 5)
                self.assertEqual(error.exception.code, code)
                self.assertEqual(opened.call_count, 1)
                self.assertFalse(self.fetch_calls)
                self.assert_head(self.base)
        for status in (401, 403, 404, 302):
            denied = urllib.error.HTTPError('https://api.gitverse.ru', status, 'denied', {}, None)
            with self.subTest(status=status), patch.object(client.opener, 'open', side_effect=denied) as opened:
                with self.assertRaises(GVError) as error:
                    checkout_pr(args, client, 'owner/repo', 5)
                self.assertEqual(error.exception.status, status)
                self.assertEqual(opened.call_count, 1)
                self.assertFalse(self.fetch_calls)
                self.assert_head(self.base)

    def test_mismatched_clone_stops_before_api(self):
        self.git('remote', 'remove', 'verse')
        with self.assertRaises(GVError) as error:
            self.checkout()
        self.assertEqual(error.exception.code, 'repo_mismatch')
        self.client.request.assert_not_called()

    def test_fork_source_uses_source_repository_without_rewriting_remotes(self):
        self.client.request.return_value['head']['repo']['full_name'] = 'fork-owner/fork-repo'
        self.checkout()
        self.assertIn('https://gitverse.ru/fork-owner/fork-repo.git', self.fetch_calls[0])
        self.assertEqual(self.git('remote'), 'origin\nverse')

    def test_fetch_failure_does_not_switch_or_leave_refs(self):
        def fail_fetch(*args, **kwargs):
            if args[0] == 'fetch':
                raise GVError('Fetch unavailable', 'network_error')
            return git_command(*args, **kwargs)
        with patch('gitverse_cli.checkout.git_command', side_effect=fail_fetch):
            with self.assertRaises(GVError):
                self.checkout()
        self.assert_head(self.base)

    def test_changes_created_during_fetch_are_preserved(self):
        def edit_after_fetch(*args, **kwargs):
            if args[0] == 'fetch':
                argv = list(args)
                argv[argv.index('--') + 1] = str(self.source)
                result = git_command(*argv, **kwargs)
                (self.clone / 'base.txt').write_text('edited while fetching\n')
                return result
            return git_command(*args, **kwargs)
        with patch('gitverse_cli.checkout.git_command', side_effect=edit_after_fetch):
            with self.assertRaises(GVError) as error:
                self.checkout()
        self.assertEqual(error.exception.code, 'dirty_worktree')
        self.assert_head(self.base)
        self.assertEqual((self.clone / 'base.txt').read_text(), 'edited while fetching\n')

    def test_force_resets_diverged_branch_and_reports_recovery_sha(self):
        self.git('switch', '-qc', 'review')
        (self.clone / 'local.txt').write_text('local')
        self.git('add', '.')
        self.git('commit', '-qm', 'local')
        old = self.git('rev-parse', 'HEAD')
        result = self.checkout('--branch', 'review', '--force')
        self.assertEqual(result['action'], 'reset')
        self.assertEqual(result['previous_target_sha'], old)
        self.assert_head(self.sha)
        self.assertEqual(self.checkout('--branch', 'review', '--force')['action'], 'already_current')
        self.assertEqual(self.git('cat-file', '-t', old), 'commit')

    def test_force_still_refuses_dirty_worktree(self):
        (self.clone / 'base.txt').write_text('keep')
        with self.assertRaises(GVError) as error:
            self.checkout('--force')
        self.assertEqual(error.exception.code, 'dirty_worktree')
        self.client.request.assert_not_called()

    def test_force_detach_leaves_existing_target_branch_unchanged(self):
        self.git('branch', 'feature/test', self.base)
        for options in ((), ('--worktree', str(self.root / 'detached-force'))):
            with self.subTest(options=options):
                result = self.checkout('--force', '--detach', *options)
                self.assertEqual(result['action'], 'detached')
                self.assertIsNone(result['branch'])
                self.assertEqual(self.git('rev-parse', 'feature/test'), self.base)
                self.assert_head(self.sha)
                if options:
                    self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.root / 'detached-force'), self.sha)

    def test_force_default_branch_resets_only_target_from_another_branch(self):
        self.git('branch', 'feature/test', self.base)
        result = self.checkout('-f')
        self.assertEqual(result['branch'], 'feature/test')
        self.assertEqual(result['action'], 'reset')
        self.assertEqual(result['previous_target_sha'], self.base)
        self.assertEqual(self.git('rev-parse', 'main'), self.base)
        self.assert_head(self.sha)

    def test_force_creates_missing_branch(self):
        result = self.checkout('--branch', 'review-new', '--force')
        self.assertEqual(result['action'], 'created')
        self.assertIsNone(result['previous_target_sha'])
        self.assert_head(self.sha)

    def test_force_dry_run_real_client_is_offline_and_preserves_branch(self):
        from gitverse_cli.client import Client
        self.client = Client(dry_run=True)
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')), \
                patch('gitverse_cli.checkout.credentials', side_effect=AssertionError('credentials forbidden')):
            result = self.checkout('--force', '--detach', '--dry-run')
        self.assertTrue(result['local_checkout']['detach'])
        self.assertEqual(result['pull_request']['method'], 'GET')
        self.assert_head(self.base)

    def test_worktree_new_branch_and_detached_leave_original_unchanged(self):
        for options in (('--branch', 'review'), ('--detach',)):
            path = self.root / ('wt-' + str(len(options)))
            result = self.checkout('--worktree', str(path), *options)
            self.assertEqual(result['worktree'], str(path))
            self.assertEqual(self.git('rev-parse', 'HEAD', cwd=path), self.sha)
            self.assertEqual(self.git('branch', '--show-current'), 'main')
            self.assert_head(self.base)

    def test_worktree_existing_branch_fast_forward_and_force(self):
        self.git('branch', 'review', self.base)
        path = self.root / 'review-wt'
        result = self.checkout('--worktree', str(path), '--branch', 'review')
        self.assertEqual(result['action'], 'fast_forwarded')
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=path), self.sha)
        self.assert_head(self.base)
        self.git('worktree', 'remove', str(path))
        self.git('branch', '-f', 'review', self.base)
        result = self.checkout('--worktree', str(path), '--branch', 'review', '--force')
        self.assertEqual(result['action'], 'reset')
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=path), self.sha)
        self.assert_head(self.base)

    def test_worktree_existing_path_is_never_reused(self):
        path = self.root / 'existing'
        path.mkdir()
        with self.assertRaises(GVError) as error:
            self.checkout('--worktree', str(path))
        self.assertEqual(error.exception.code, 'destination_exists')
        self.client.request.assert_not_called()

    def test_worktree_invalid_path_refuses_before_network_including_branch_preview(self):
        for path in ('', 'bad\x00path'):
            for selector in ('5', 'feature/test'):
                for preview in ([], ['--dry-run']):
                    with self.subTest(path=path, selector=selector, preview=preview), \
                            patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')):
                        with self.assertRaises(GVError) as error:
                            execute(parser().parse_args(['pr', 'checkout', selector, '-R', 'owner/repo', '--worktree', path, *preview]))
                        self.assertEqual(error.exception.code, 'usage_error')
        self.assert_head(self.base)

    def test_worktree_relative_path_default_branch_and_existing_equal_sha(self):
        path = self.clone.parent / 'relative wt'
        self.git('branch', 'feature/test', self.sha)
        result = self.checkout('--worktree', '../relative wt')
        self.assertEqual(result['action'], 'already_current')
        self.assertEqual(result['worktree'], str(path.resolve()))
        self.assertEqual(self.git('symbolic-ref', 'HEAD', cwd=path), 'refs/heads/feature/test')
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=path), self.sha)
        self.assert_head(self.base)

    def test_worktree_rejects_symlink_and_path_appearing_during_fetch(self):
        path = self.root / 'new-wt'
        path.symlink_to(self.root / 'nonexistent')
        with self.assertRaises(GVError) as error:
            self.checkout('--worktree', str(path))
        self.assertEqual(error.exception.code, 'destination_exists')
        self.client.request.assert_not_called()
        path.unlink()
        first = self.client.request.return_value
        def read(*args, **kwargs):
            if self.fetch_calls:
                path.mkdir()
                (path / 'preserve').write_text('concurrent')
            return first
        self.client.request.side_effect = read
        with self.assertRaises(GVError) as error:
            self.checkout('--worktree', str(path))
        self.assertEqual(error.exception.code, 'destination_exists')
        self.assertFalse(error.exception.details['local_mutation_attempted'])
        self.assertEqual((path / 'preserve').read_text(), 'concurrent')
        self.assert_head(self.base)

    def test_worktree_checked_out_branch_refuses_even_with_force(self):
        path = self.root / 'new-wt'
        with self.assertRaises(GVError) as error:
            self.checkout('--worktree', str(path), '--branch', 'main', '--force')
        self.assertEqual(error.exception.code, 'git_error')
        self.assertFalse(path.exists())
        self.assert_head(self.base)

    def test_worktree_unknown_outcome_retains_destination_and_no_retry(self):
        path = self.root / 'new-wt'
        original_side_effect = self.git_patch.target.git_command.side_effect
        calls = []
        def lost_result(*args, **kwargs):
            result = original_side_effect(*args, **kwargs)
            if args[:2] == ('worktree', 'add'):
                calls.append(args)
                raise GVError('Timed out', 'deadline_exceeded')
            return result
        with patch('gitverse_cli.checkout.git_command', side_effect=lost_result):
            with self.assertRaises(GVError) as error:
                self.checkout('--worktree', str(path))
        self.assertEqual(len(calls), 1)
        details = error.exception.details
        self.assertTrue(details['local_mutation_attempted'])
        self.assertFalse(details['mutation_attempted'])
        self.assertEqual(details['worktree'], str(path))
        self.assertEqual(details['previous_branch'], 'main')
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=path), self.sha)
        self.assert_head(self.base)

    def test_worktree_detects_concurrent_original_branch_change_at_same_sha(self):
        path = self.root / 'new-wt'
        original_side_effect = self.git_patch.target.git_command.side_effect
        def concurrent(*args, **kwargs):
            result = original_side_effect(*args, **kwargs)
            if args[:2] == ('worktree', 'add'):
                self.git('switch', '-qc', 'concurrent')
            return result
        with patch('gitverse_cli.checkout.git_command', side_effect=concurrent):
            with self.assertRaises(GVError) as error:
                self.checkout('--worktree', str(path))
        self.assertEqual(error.exception.code, 'original_head_changed')
        self.assertEqual(error.exception.details['stage'], 'verify')
        self.assertEqual(error.exception.details['previous_branch'], 'main')
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=path), self.sha)
        self.assert_head(self.base)

    def test_worktree_offline_preview_runs_neither_http_nor_git(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')), \
                patch('gitverse_cli.checkout.git_command', side_effect=AssertionError('git forbidden')):
            result = execute(parser().parse_args(['pr', 'checkout', '5', '-R', 'owner/repo', '--worktree', 'new', '--detach', '--dry-run']))
        self.assertEqual(result['local_checkout']['worktree'], 'new')
        self.assertTrue(result['local_checkout']['detach'])
        self.assertFalse((self.clone / 'new').exists())

    def test_worktree_refuses_checked_out_branch_even_with_force(self):
        path = self.root / 'other'
        self.git('worktree', 'add', '-b', 'review', str(path), self.base)
        with self.assertRaises(GVError) as error:
            self.checkout('--branch', 'review', '--force')
        self.assertEqual(error.exception.code, 'git_error')
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=path), self.base)
        self.assert_head(self.base)
        self.assertEqual(error.exception.details['previous_target_sha'], self.base)

    def test_malformed_or_wrong_scope_dto_refuses_before_fetch(self):
        import copy
        original = copy.deepcopy(self.client.request.return_value)
        variants = [[], {}, {**original, 'number': 6}, {**original, 'id': True},
                    {**original, 'head': []}, {**original, 'base': {'repo': {'full_name': []}}},
                    {**original, 'base': {'repo': {'full_name': 'other/repo'}}},
                    {**original, 'head': {**original['head'], 'sha': 'bad'}},
                    {**original, 'head': {**original['head'], 'repo': {'full_name': 'https://evil.test/o/r'}}}]
        for value in variants:
            with self.subTest(value=value):
                self.client.request.return_value = value
                with self.assertRaises(GVError):
                    self.checkout()
                self.assertFalse(self.fetch_calls)
                self.assert_head(self.base)

    def test_pr_changed_during_fetch_refuses_checkout(self):
        import copy
        first = copy.deepcopy(self.client.request.return_value)
        second = copy.deepcopy(first)
        second['head']['sha'] = self.base
        self.client.request.side_effect = [first, second]
        with self.assertRaises(GVError) as error:
            self.checkout('--force')
        self.assertEqual(error.exception.code, 'pr_changed')
        self.assertFalse(error.exception.details['local_mutation_attempted'])
        self.assert_head(self.base)

    def test_permission_denial_is_not_retried_by_checkout(self):
        self.client.request.side_effect = GVError('Forbidden', 'http_error', 403)
        with self.assertRaises(GVError):
            self.checkout()
        self.assertEqual(self.client.request.call_count, 1)
        self.assertFalse(self.fetch_calls)

    def test_identity_or_source_change_during_fetch_preserves_head(self):
        import copy
        first = copy.deepcopy(self.client.request.return_value)
        for field in ('id', 'source', 'ref', 'base'):
            second = copy.deepcopy(first)
            if field == 'id': second['id'] += 1
            elif field == 'source': second['head']['repo']['full_name'] = 'other/fork'
            elif field == 'ref': second['head']['ref'] = 'other-branch'
            else: second['base']['repo']['full_name'] = 'other/repo'
            with self.subTest(field=field):
                self.client.request.side_effect = [first, second]
                with self.assertRaises(GVError):
                    self.checkout()
                self.assert_head(self.base)
                self.assertNotIn('refs/heads/feature/test', self.git('show-ref', '--heads', cwd=self.clone))

    def test_real_transport_rejects_partial_oversize_and_redirect_before_fetch(self):
        class Response(io.BytesIO):
            status = 200
        for kind in ('partial', 'oversize', 'redirect', 'unauthorized', 'forbidden'):
            client = Client(dry_run=True)
            client.dry_run = False
            client.opener = Mock()
            if kind in ('redirect', 'unauthorized', 'forbidden'):
                status = {'redirect': 302, 'unauthorized': 401, 'forbidden': 403}[kind]
                client.opener.open.side_effect = urllib.error.HTTPError(
                    client.base, status, 'refused', {'Location': 'https://other.invalid/secret'}, None)
                expected = 'http_error'
            else:
                response = Response(b'{}' if kind == 'partial' else b'x' * 1048577)
                response.status = 206 if kind == 'partial' else 200
                client.opener.open.return_value = response
                expected = 'incomplete_response' if kind == 'partial' else 'response_limit'
            with self.subTest(kind=kind):
                args = parser().parse_args(['pr', 'checkout', '5', '-R', 'owner/repo'])
                with self.assertRaises(GVError) as error:
                    checkout_pr(args, client, 'owner/repo', 5)
                self.assertEqual(error.exception.code, expected)
                self.assertEqual(client.opener.open.call_count, 1)
                self.assertFalse(self.fetch_calls)
                self.assert_head(self.base)

    def test_actual_offline_preview_has_no_http_or_git_mutation(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')), \
                patch('gitverse_cli.checkout.git_command', side_effect=AssertionError('git forbidden')):
            result = execute(parser().parse_args(['pr', 'checkout', '5', '-R', 'owner/repo', '--dry-run']))
        self.assertEqual(result['pull_request']['method'], 'GET')
        self.assertEqual(result['local_checkout']['branch'], '<head.ref>')
        self.assert_head(self.base)

    def test_unknown_local_checkout_result_preserves_recovery_details(self):
        def fail_verify(*args, **kwargs):
            if args[:2] == ('rev-parse', 'HEAD'):
                raise GVError('Timed out', 'deadline_exceeded')
            if args[0] == 'fetch':
                args = list(args)
                args[args.index('--') + 1] = str(self.source)
            return git_command(*args, **kwargs)
        with patch('gitverse_cli.checkout.git_command', side_effect=fail_verify):
            with self.assertRaises(GVError) as error:
                self.checkout('--force')
        self.assertTrue(error.exception.details['local_mutation_attempted'])
        self.assertFalse(error.exception.details['mutation_attempted'])
        self.assertEqual(error.exception.details['stage'], 'verify')
        self.assertEqual(error.exception.details['previous_sha'], self.base)
        self.assert_head(self.sha)

    def test_force_worktree_preview_never_runs_git(self):
        self.client.dry_run = True
        with patch('gitverse_cli.checkout.git_command', return_value=Mock(returncode=0, stdout='review\n')) as command:
            result = self.checkout('--branch', 'review', '--force', '--worktree', 'new', '--dry-run')
        # Branch syntax validation is local; no fetch/switch/worktree command.
        self.assertEqual([call.args[0] for call in command.call_args_list], ['check-ref-format'])
        self.assertTrue(result['local_checkout']['force'])
        self.assertEqual(result['local_checkout']['worktree'], 'new')

    def test_force_keeps_ignored_collision(self):
        (self.clone / '.git' / 'info' / 'exclude').write_text('feature.txt\n')
        (self.clone / 'feature.txt').write_text('keep ignored')
        with self.assertRaises(GVError):
            self.checkout('--force')
        self.assertEqual((self.clone / 'feature.txt').read_text(), 'keep ignored')
        self.assert_head(self.base)

    def test_read_requests_are_complete_bounded_and_share_deadline(self):
        self.checkout()
        calls = self.client.request.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs['deadline'], calls[1].kwargs['deadline'])
        self.assertEqual(calls[0].kwargs['max_response_bytes'], 1048576)
        self.assertTrue(calls[0].kwargs['require_complete'])

    def test_expired_deadline_returns_json_error_before_mutation(self):
        with patch('gitverse_cli.checkout.time.monotonic', side_effect=[0, 1000]):
            with self.assertRaises(GVError) as error:
                self.checkout('--timeout', '1')
        self.assertEqual(error.exception.code, 'deadline_exceeded')
        self.client.request.assert_not_called()
        self.assert_head(self.base)

    def test_git_timeout_is_sanitized(self):
        with patch('gitverse_cli.checkout.subprocess.run', side_effect=subprocess.TimeoutExpired(['git'], 1, stderr='SECRET')):
            with self.assertRaises(GVError) as error:
                git_command('fetch')
        self.assertEqual(error.exception.code, 'deadline_exceeded')
        self.assertNotIn('SECRET', str(error.exception.as_dict()))

    def test_url_selects_repo_and_rejects_conflicting_repo(self):
        url = 'https://gitverse.ru/owner/repo/pulls/5#discussion'
        self.assertEqual(pr_reference(url), (5, 'owner/repo'))
        with self.assertRaises(GVError):
            pr_reference(url, 'other/repo')
        for value in ('0', '-1', 'https://evil.test/owner/repo/pulls/5', 'https://user@gitverse.ru/owner/repo/pulls/5'):
            with self.assertRaises(GVError):
                pr_reference(value)

    def test_dry_run_does_not_fetch_or_require_worktree(self):
        self.client.dry_run = True
        self.client.request.return_value = {'method': 'GET'}
        result = self.checkout('--detach')
        self.assertEqual(result['pull_request'], {'method': 'GET'})
        self.assertFalse(self.fetch_calls)


class CredentialTests(unittest.TestCase):
    def test_checkout_settings_disable_redirects_and_scope_helper(self):
        from gitverse_cli.checkout import credentials
        client = Mock(auth_source='config')
        client.request.return_value = {'login': 'test-user'}
        with patch.dict(os.environ, {'GIT_TRACE_CURL': '1', 'GIT_CURL_VERBOSE': '1'}):
            settings, env = credentials(client, 'owner/repo', deadline=123)
        self.assertIn('http.followRedirects=false', settings)
        self.assertIn('credential.helper=', settings)
        self.assertIn('credential.useHttpPath=true', settings)
        self.assertIn('protocol.allow=never', settings)
        self.assertIn('http.sslVerify=true', settings)
        self.assertEqual(env['GV_GIT_HELPER_REPO'], 'owner/repo')
        self.assertNotIn('GIT_TRACE_CURL', env)
        self.assertNotIn('GIT_CURL_VERBOSE', env)
        self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')
        self.assertEqual(client.request.call_args.kwargs['max_response_bytes'], 1048576)

    def test_helper_never_answers_other_hosts_paths_or_store_requests(self):
        env = {'GV_GIT_HELPER_HOST': 'gitverse.ru', 'GV_GIT_HELPER_REPO': 'owner/repo', 'GV_GIT_HELPER_USERNAME': 'user'}
        request = {'protocol': 'https', 'host': 'gitverse.ru', 'path': 'owner/repo.git'}
        with patch.dict(os.environ, env, clear=True), patch('gitverse_cli.git_credential.Client') as factory:
            factory.return_value.headers = {'Authorization': 'Bearer SECRET'}
            self.assertEqual(answer('get', request), 'username=user\npassword=SECRET\n\n')
            for change in ({'protocol': 'http'}, {'host': 'evil.test'}, {'path': 'other/repo.git'}):
                self.assertEqual(answer('get', {**request, **change}), '')
            self.assertEqual(answer('store', request), '')
            self.assertEqual(answer('erase', request), '')
            self.assertEqual(factory.call_count, 1)


if __name__ == '__main__':
    unittest.main()
