"""Verbose defaults with real Git histories and synthetic public transport."""
import io
from pathlib import Path
from unittest.mock import patch

import test_pr_create_fill as shared
from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError


class CreateFillVerboseTests(shared.CreateFillTests):
    # Exercise shared scope, bounds, concurrency, unknown writes, readback and
    # redirect cases through this flag, not only through its sibling --fill.
    def call(self, *flags):
        args = parser().parse_args(['pr', 'create', '-R', shared.REPO,
                                   '--head', 'feature', '--base', 'main', '--fill-verbose', *flags])
        with patch('gitverse_cli.cli.Client', return_value=self.c):
            return execute(args)

    def test_multi_commit_branch_humanization_oldest_first_subjects_only(self):
        self.git('branch', '-m', 'feature', 'fix/a_b-c')
        self.commit('Second **literal**', 'Second body\n\n- nested 😀')
        self.reset_client()
        self.c.pull['head']['ref'] = 'fix/a_b-c'
        row = self.call('--head', 'fix/a_b-c')
        self.assertEqual(row['title'], 'fix/a b c')
        self.assertEqual(row['body'], '- **Subject 😀**\n  Body\n  \n  Literal $HOME\n  \n\n'
                                     '- **Second **literal****\n  Second body\n  \n  - nested 😀\n  ')
        self.assertEqual(row['fill']['commits'], 2)
        self.assertEqual(row['fill']['mode'], 'verbose')

    def test_merge_commits_are_excluded_and_diverged_base_is_not_a_subject(self):
        self.git('checkout', '-q', '-b', 'side', self.base)
        self.commit('Side subject', 'Side body')
        self.git('checkout', '-q', 'feature')
        self.git('merge', '--no-ff', '-m', 'Excluded merge', 'side')
        self.git('checkout', '-q', 'main')
        self.commit('Excluded base', 'Excluded base body')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.git('update-ref', 'refs/remotes/origin/main', self.base)
        self.git('checkout', '-q', 'feature')
        self.head = self.git('rev-parse', 'HEAD').strip()
        self.reset_client()
        row = self.call()
        self.assertEqual(row['fill']['commits'], 2)
        self.assertIn('- **Side subject**\n  Side body\n  ', row['body'])
        self.assertIn('- **Subject 😀**\n  Body\n  \n  Literal $HOME\n  ', row['body'])
        self.assertNotIn('Excluded', row['body'])

    def test_short_alias_and_current_branch_and_configured_base(self):
        self.git('config', 'branch.feature.gh-merge-base', 'main')
        args = parser().parse_args(['pr', 'create', '-R', shared.REPO, '--fill-verbose'])
        with patch('gitverse_cli.cli.Client', return_value=self.c):
            row = execute(args)
        self.assertEqual(row['head']['ref'], 'feature')
        self.assertEqual(row['fill']['mode'], 'verbose')

    def test_default_base_resolved_without_affecting_fill(self):
        args = parser().parse_args(['pr', 'create', '-R', shared.REPO, '--fill-verbose', '--head', 'feature'])
        with patch('gitverse_cli.cli.Client', return_value=self.c):
            row = execute(args)
        self.assertEqual(row['base']['ref'], 'main')
        self.assertEqual(row['fill']['mode'], 'verbose')

    def test_fill_modes_are_mutually_exclusive_before_network(self):
        for flag in ('--fill', '-f', '--fill-first'):
            with self.assertRaises(GVError), patch('sys.stderr', io.StringIO()):
                self.call(flag)
        self.assertEqual(self.c.calls, [])

    def test_multi_commit_overrides_and_editor_seed_use_verbose_body(self):
        self.commit('Later title', 'Later body')
        verbose = '- **Subject 😀**\n  Body\n  \n  Literal $HOME\n  \n\n- **Later title**\n  Later body\n  '
        for flags, title, body in [(['--title', 'Explicit'], 'Explicit', verbose),
                                   (['--body', ''], 'feature', ''),
                                   (['--title', 'Explicit', '--body', ''], 'Explicit', '')]:
            self.reset_client()
            row = self.call(*flags)
            self.assertEqual((row['title'], row['body']), (title, body))
        self.reset_client()
        with patch('gitverse_cli.comment_editor.edit_text', return_value='Edited\nFinal') as editor:
            self.call('--editor')
        self.assertEqual(editor.call_args.args[0], 'feature\n' + verbose)

    def test_empty_bodies_indent_even_empty_final_line_and_preserve_separators(self):
        self.git('reset', '--hard', self.base)
        self.commit('First no body')
        self.reset_client()
        self.assertEqual(self.call()['body'], '')  # Single commit stays literal.
        self.commit('Second no body')
        self.commit('Third', 'Last body')
        self.reset_client()
        self.assertEqual(self.call()['body'], '- **First no body**\n  \n\n'
                                             '- **Second no body**\n  \n\n'
                                             '- **Third**\n  Last body\n  ')

    def test_verbatim_body_crlf_tabs_spaces_and_missing_final_lf(self):
        Path('message').write_bytes(b'Verbatim\n\n First\r\n\tSecond\n\nTail  ')
        self.git('commit', '-q', '--allow-empty', '--cleanup=verbatim', '-F', 'message')
        self.head = self.git('rev-parse', 'HEAD').strip()
        self.reset_client()
        self.assertEqual(self.call()['body'], '- **Subject 😀**\n  Body\n  \n  Literal $HOME\n  \n\n'
                                             '- **Verbatim**\n   First\r\n  \tSecond\n  \n  Tail  ')

    def test_verbose_indentation_can_exceed_final_body_limit(self):
        # Git bytes fit4MiB; indentation alone pushes final wire body past1MiB.
        Path('message').write_bytes(b'Large\n\n' + b'x\n' * 270000)
        self.git('commit', '-q', '--allow-empty', '--cleanup=verbatim', '-F', 'message')
        self.head = self.git('rev-parse', 'HEAD').strip()
        self.reset_client()
        with self.assertRaises(GVError) as error:
            self.call()
        self.assertEqual(error.exception.code, 'usage_error')
        self.assertEqual(self.c.calls, [])

    def test_even_explicit_fields_require_complete_fill_history(self):
        self.git('update-ref', '-d', 'refs/remotes/origin/main')
        with self.assertRaises(GVError):
            self.call('--title', 'Explicit', '--body', 'Explicit')
        self.assertEqual(self.c.calls, [])
