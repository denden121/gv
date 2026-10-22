import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from gitverse_cli.cli import execute, main, parser
from gitverse_cli.client import GVError
from gitverse_cli.checks import ChecksResult
from gitverse_cli.pulls import branch_spec, local_head, matching, pr_status, resolve_branch, scan_pulls


REPO = 'owner/repo'


def pull(number=7, branch='feature/test', source=REPO, author='me', reviewers=(), state='open'):
    return {'number': number, 'state': state, 'title': 'Change', 'draft': False, 'head': {'ref': branch, 'sha': 'a' * 40, 'repo': {'full_name': source}},
            'base': {'ref': 'main'}, 'user': {'login': author}, 'requested_reviewers': [{'login': r} for r in reviewers], 'requested_teams': []}


class PullTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def client(self, *responses):
        c = Mock(backend='public', dry_run=False)
        c.request.side_effect = copy.deepcopy(responses)
        return c

    def status(self, c, *flags, context=None):
        args = parser().parse_args(['pr', 'status', '-R', REPO, *flags])
        with patch('gitverse_cli.pulls.local_head', return_value=context):
            return pr_status(args, c, REPO)

    def test_plain_and_qualified_branches(self):
        self.assertEqual(branch_spec('feat/one')['branch'], 'feat/one')
        self.assertEqual(branch_spec('other:feat/one')['owner'], 'other')
        self.assertEqual(branch_spec('other:fork:feat/one')['repository'], 'other/fork')
        for value in ('', ':branch', 'a:b:c:d', '-branch', 'x:../bad', '..:branch', 'a::branch'):
            with self.assertRaises(GVError):
                branch_spec(value)

    def test_exact_head_and_source_matching_excludes_closed(self):
        rows = [pull(), pull(8, source='other/fork'), pull(9, branch='feature/test-more'), pull(10, state='closed')]
        self.assertEqual([p['number'] for p in matching(rows, branch_spec('feature/test'))], [7, 8])
        self.assertEqual([p['number'] for p in matching(rows, branch_spec('other:fork:feature/test'))], [8])
        self.assertEqual([p['number'] for p in matching(rows, branch_spec('owner:feature/test'))], [7])

    def test_unique_branch_resolves_only_after_empty_page(self):
        c = self.client([pull()], [])
        self.assertEqual(resolve_branch(c, REPO, 'feature/test'), 7)
        self.assertEqual(c.request.call_count, 2)
        self.assertNotIn('head', c.request.call_args_list[0].args[2])

    def test_second_page_ambiguity_never_returns_first(self):
        c = self.client([pull()], [pull(8, source='other/fork')], [])
        with self.assertRaises(GVError) as e:
            resolve_branch(c, REPO, 'feature/test')
        self.assertEqual(e.exception.code, 'ambiguous_pr')
        self.assertEqual(e.exception.details['numbers'], [7, 8])

    def test_no_match_and_closed_only_are_errors(self):
        for responses in (([],), ([pull(state='closed')], []), ([pull(branch='different')], [])):
            with self.assertRaises(GVError) as e:
                resolve_branch(self.client(*responses), REPO, 'feature/test')
            self.assertEqual(e.exception.code, 'pr_not_found')

    def test_server_filter_is_not_trusted(self):
        c = self.client([pull(9, branch='other'), pull(8, source='other/fork'), pull()], [])
        self.assertEqual(resolve_branch(c, REPO, 'owner:repo:feature/test'), 7)

    def test_current_branch_scopes_to_full_source_repository(self):
        context = {'local_branch': 'local-name', 'branch': 'remote-name', 'repository': 'other/fork', 'source': 'upstream'}
        c = self.client([pull(8, 'remote-name', source='other/fork'), pull(9, 'remote-name')], [])
        with patch('gitverse_cli.pulls.local_head', return_value=context):
            self.assertEqual(resolve_branch(c, REPO, None), 8)
        self.assertNotIn('head', c.request.call_args_list[0].args[2])

    def test_missing_source_or_head_never_guesses(self):
        for p in ({**pull(), 'head': {}}, pull(source=None), {**pull(), 'head': {'ref': 'feature/test', 'repo': 'invalid'}}):
            c = self.client([p], [])
            with self.assertRaises(GVError) as e:
                resolve_branch(c, REPO, 'owner:repo:feature/test')
            self.assertEqual(e.exception.code, 'unexpected_response')

    def test_pagination_repeats_bounds_invalid_and_early_empty(self):
        cases = [([pull()], [pull()]), ([pull()],), ({'items': [pull()], 'total_count': 2}, {'items': [], 'total_count': 2}),
                 ({'items': [], 'total_count': -1},), ({'items': [pull()], 'total_count': 0},), ([{'number': True, 'state': 'open'}],), ([pull(state='unknown')],), ({'unexpected': []},)]
        for responses in cases:
            c = self.client(*responses)
            with self.assertRaises(GVError):
                scan_pulls(c, REPO, {}, max_pages=len(responses))

    def test_total_count_supports_capped_pages(self):
        c = self.client({'items': [pull()], 'total_count': 2}, {'items': [pull(8)], 'total_count': 2})
        self.assertEqual(len(scan_pulls(c, REPO, {})), 2)

    def test_url_all_operations_skip_branch_lookup(self):
        for action, options in [('view', []), ('files', []), ('commits', []), ('reviews', []), ('comments', []), ('context', []), ('diff', []), ('edit', ['--title', 'New']),
                                ('close', []), ('reopen', []), ('update-branch', []), ('review', ['--comment', '--body', 'Text', '--commit', 'a' * 40]),
                                ('comment', ['--body', 'Text']), ('resolve', ['1', '--backend', 'web']), ('merge', ['--backend', 'web', '--match-head-commit', 'a' * 40, '--title', 'Merge'])]:
            args = parser().parse_args(['pr', action, 'https://gitverse.ru/owner/repo/pulls/7', *options, '--dry-run'])
            with patch('gitverse_cli.pulls.resolve_branch', side_effect=AssertionError('URL needs no search')):
                execute(args)
            self.assertEqual(args.number, 7)
            self.assertEqual(args.repo, REPO)

    def test_branch_write_ambiguity_sends_no_patch(self):
        c = self.client([pull(), pull(8)], [])
        with patch('gitverse_cli.cli.Client', return_value=c), self.assertRaises(GVError):
            execute(parser().parse_args(['pr', 'close', 'feature/test', '-R', REPO]))
        self.assertTrue(all(call.args[0] == 'GET' for call in c.request.call_args_list))

    def test_qualified_branch_edit_uses_selected_number(self):
        c = self.client([pull(8, source='other/fork')], [], {'updated': True})
        with patch('gitverse_cli.cli.Client', return_value=c):
            result = execute(parser().parse_args(['pr', 'edit', 'other:fork:feature/test', '-R', REPO, '--title', 'New']))
        self.assertTrue(result['updated'])
        self.assertEqual(c.request.call_args.args, ('PATCH', 'repos/owner/repo/pulls/8'))

    def test_dry_run_branch_is_an_unresolved_offline_plan(self):
        result = execute(parser().parse_args(['pr', 'close', 'feature/test', '-R', REPO, '--dry-run']))
        self.assertIn('selection', result)
        self.assertNotIn('body', result)
        self.assertEqual(result['selection']['request']['method'], 'GET')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            main(['pr', 'resolve', 'api', '123', '-R', REPO, '--backend', 'web', '--dry-run'])
        self.assertEqual(json.loads(output.getvalue())['action'], 'resolve')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            main(['-R', REPO, 'api', 'repos/{owner}/{repo}', '--dry-run'])
        self.assertEqual(json.loads(output.getvalue())['url'], 'https://api.gitverse.ru/repos/owner/repo')

    def test_web_branch_selection_uses_separate_public_client(self):
        web = Mock(backend='web', dry_run=False)
        public = self.client([pull()], [])
        with patch('gitverse_cli.cli.Client', side_effect=[web, public]) as factory:
            execute(parser().parse_args(['pr', 'threads', 'feature/test', '-R', REPO, '--backend', 'web']))
        self.assertEqual(factory.call_args.args, ('public', False))
        web.request.assert_called_once_with('GET', 'repos/owner/repo/pulls/7/comments')
        self.assertTrue(all(call.args[0] == 'GET' for call in public.request.call_args_list))

    def test_no_selector_uses_current_branch(self):
        c = self.client([pull()], [], pull())
        with patch('gitverse_cli.cli.Client', return_value=c), patch('gitverse_cli.pulls.local_head', return_value={'branch': 'feature/test', 'repository': REPO}):
            result = execute(parser().parse_args(['pr', 'view', '-R', REPO]))
        self.assertEqual(result['number'], 7)

    def test_resolve_optional_selector_preserves_comment_id(self):
        args = parser().parse_args(['pr', 'resolve', '123', '-R', REPO])
        self.assertIsNone(args.selector)
        self.assertEqual(args.comment_id, 123)
        args = parser().parse_args(['pr', 'resolve', '7', '123', '-R', REPO])
        self.assertEqual(args.selector, '7')
        args = parser().parse_args(['pr', 'resolve', 'feature/test', '-R', REPO, '--backend', 'web', '--dry-run', '123'])
        self.assertEqual((args.selector, args.comment_id), ('feature/test', 123))
        for values in ([], ['7', '0'], ['7', '123', '999'], ['feature/test', 'bad']):
            with self.assertRaises(GVError):
                parser().parse_args(['pr', 'resolve', *values])

    def test_status_groups_current_author_and_direct_review_requests(self):
        rows = [pull(), pull(8, source='other/fork', author='other', reviewers=['ME']), pull(9, branch='elsewhere', author='other')]
        c = self.client({'login': 'me'}, rows, [])
        result = self.status(c, context={'branch': 'feature/test', 'repository': REPO})
        self.assertEqual([p['number'] for p in result['current_branch']['pull_requests']], [7])
        self.assertEqual([p['number'] for p in result['created_by_you']], [7])
        self.assertEqual([p['number'] for p in result['review_requested']], [8])
        self.assertFalse(result['coverage']['team_membership_known'])
        self.assertIsNone(result['review_requested'][0]['ci'])
        self.assertIsNone(result['created_by_you'][0]['review_decision'])

    def test_status_without_git_still_lists_personal_prs(self):
        args = parser().parse_args(['pr', 'status', '-R', REPO])
        c = self.client({'login': 'me'}, [], [])
        # Empty page ends the scan; the extra response must remain unused.
        with patch('gitverse_cli.pulls.local_head', side_effect=GVError('Detached', 'no_current_branch')):
            result = pr_status(args, c, REPO)
        self.assertEqual(result['current_branch']['reason'], 'no_current_branch')
        self.assertEqual(result['created_by_you'], [])

    def test_status_ambiguous_current_lists_all_candidates(self):
        result = self.status(self.client({'login': 'me'}, [pull(), pull(8)], []), context={'branch': 'feature/test', 'repository': REPO})
        self.assertTrue(result['current_branch']['ambiguous'])
        self.assertEqual(len(result['current_branch']['pull_requests']), 2)

    def test_status_invalid_user_or_reviewer_does_not_claim_complete(self):
        for responses in (({},), ({'login': 'me'}, [{**pull(), 'requested_reviewers': None}], []), ({'login': 'me'}, [{**pull(), 'user': 'invalid'}], []), ({'login': 'me'}, [{**pull(), 'head': 'invalid'}], [])):
            with self.assertRaises(GVError):
                self.status(self.client(*responses))

    def test_status_checks_enriches_each_unique_pr_once_and_flags_stale_list(self):
        c = self.client({'login': 'me'}, [pull(reviewers=['me'])], [])
        response = ChecksResult({'head_sha': 'b' * 40, 'ci_state': 'pass', 'exit_code': 0, 'coverage': {'scope_complete': True}}, 0)
        with patch('gitverse_cli.checks.pr_checks', return_value=response) as checks:
            result = self.status(c, '--checks', context={'branch': 'feature/test', 'repository': REPO})
        checks.assert_called_once()
        ci = result['created_by_you'][0]['ci']
        self.assertTrue(ci['listed_head_changed'])
        self.assertFalse(ci['coverage']['scope_complete'])

    def test_status_checks_limit_refuses_truncation(self):
        c = self.client({'login': 'me'}, [pull(), pull(8)], [])
        with self.assertRaises(GVError) as e:
            self.status(c, '--checks', '--checks-limit', '1')
        self.assertEqual(e.exception.code, 'checks_limit')

    def test_status_dry_run_does_not_fetch_or_require_git(self):
        with patch('gitverse_cli.pulls.local_head', side_effect=GVError('No Git', 'git_error')):
            result = execute(parser().parse_args(['pr', 'status', '-R', REPO, '--dry-run']))
        self.assertEqual(result['user_request']['method'], 'GET')
        self.assertEqual(result['current_branch_reason'], 'git_error')

    def test_status_json_fields(self):
        c = self.client({'login': 'me'}, [])
        with patch('gitverse_cli.cli.Client', return_value=c), patch('gitverse_cli.pulls.local_head', return_value=None), contextlib.redirect_stdout(io.StringIO()) as output:
            main(['pr', 'status', '-R', REPO, '--fields', 'login,open_prs_scanned'])
        self.assertEqual(json.loads(output.getvalue()), {'login': 'me', 'open_prs_scanned': 0})

    def test_list_local_head_base_and_state_filters_keep_page_semantics(self):
        rows = [pull(7), pull(8), pull(9, source='other/fork'), pull(10, state='closed')]
        c = self.client(rows, [])
        with patch('gitverse_cli.cli.Client', return_value=c):
            result = execute(parser().parse_args(['pr', 'list', '-R', REPO, '--head', 'owner:repo:feature/test', '--base', 'main', '--state', 'all', '--limit', '1', '--page', '2', '--paginate']))
        self.assertEqual([p['number'] for p in result], [8, 7])
        self.assertNotIn('head', c.request.call_args_list[0].args[2])
        self.assertNotIn('base', c.request.call_args_list[0].args[2])
        c = self.client([{**pull(), 'base': 'invalid'}], [])
        with patch('gitverse_cli.cli.Client', return_value=c), self.assertRaises(GVError):
            execute(parser().parse_args(['pr', 'list', '-R', REPO, '--base', 'main']))


class LocalHeadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, self.old)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        self.git('init', '-q', '-b', 'feature/test')
        self.git('remote', 'add', 'verse', 'https://gitverse.ru/owner/repo.git')

    def git(self, *args):
        subprocess.run(['git', *args], check=True, capture_output=True, text=True)

    def test_untracked_branch_uses_unique_gitverse_remote(self):
        self.git('remote', 'add', 'origin', 'https://example.com/other/repo.git')
        self.assertEqual(local_head()['repository'], REPO)
        self.assertEqual(local_head()['branch'], 'feature/test')

    def test_upstream_maps_local_name_to_source_branch_and_fork(self):
        self.git('remote', 'add', 'fork', 'git@gitverse.ru:other/fork.git')
        self.git('config', 'branch.feature/test.remote', 'fork')
        self.git('config', 'branch.feature/test.merge', 'refs/heads/remote-name')
        self.assertEqual(local_head(), {'local_branch': 'feature/test', 'branch': 'remote-name', 'repository': 'other/fork', 'source': 'upstream'})

    def test_ambiguous_or_non_gitverse_upstream_refuses_guessing(self):
        self.git('remote', 'add', 'fork', 'https://gitverse.ru/other/fork.git')
        with self.assertRaises(GVError) as e:
            local_head()
        self.assertEqual(e.exception.code, 'ambiguous_head_repo')
        self.git('config', 'branch.feature/test.remote', '.')
        self.git('config', 'branch.feature/test.merge', 'refs/heads/feature/test')
        with self.assertRaises(GVError):
            local_head()

    def test_detached_head_requires_explicit_selector(self):
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-qm', 'initial')
        self.git('checkout', '--detach')
        with self.assertRaises(GVError) as e:
            local_head()
        self.assertEqual(e.exception.code, 'no_current_branch')


if __name__ == '__main__':
    unittest.main()
