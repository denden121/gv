"""First-commit semantics, exercised with real Git and synthetic transport."""
import io
from unittest.mock import patch

import test_pr_create_fill as shared
from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError


class CreateFillFirstTests(shared.CreateFillTests):
    # Re-run the shared transport, scope, bounds, concurrency, no-op and
    # recovery cases through --fill-first rather than treating --fill as proof.
    def call(self, *flags):
        args = parser().parse_args(['pr', 'create', '-R', shared.REPO,
                                   '--head', 'feature', '--base', 'main', '--fill-first', *flags])
        with patch('gitverse_cli.cli.Client', return_value=self.c):
            return execute(args)

    def test_multi_commit_branch_humanization_oldest_first_subjects_only(self):
        self.commit('Second title', 'Second description')
        self.reset_client()
        row = self.call()
        self.assertEqual((row['title'], row['body']), ('Subject 😀', 'Body\n\nLiteral $HOME\n'))
        self.assertEqual(row['fill']['commits'], 2)
        self.assertEqual(row['fill']['mode'], 'first')

    def test_merge_commits_are_excluded_and_diverged_base_is_not_a_subject(self):
        self.git('checkout', '-q', '-b', 'side', self.base)
        self.commit('Side subject')
        self.git('checkout', '-q', 'feature')
        self.git('merge', '--no-ff', '-m', 'Excluded merge', 'side')
        self.head = self.git('rev-parse', 'HEAD').strip()
        self.git('checkout', '-q', 'main')
        self.commit('Excluded base')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.git('update-ref', 'refs/remotes/origin/main', self.base)
        self.git('checkout', '-q', 'feature')
        self.head = self.git('rev-parse', 'HEAD').strip()
        self.reset_client()
        first = self.git('log', '--cherry', '--format=%s', self.base+'...'+self.head).splitlines()[-1]
        row = self.call()
        self.assertEqual(row['title'], first)
        self.assertNotIn(row['title'], ('Excluded merge', 'Excluded base'))
        self.assertEqual(row['fill']['commits'], 2)

    def test_short_alias_and_current_branch_and_configured_base(self):
        self.git('config', 'branch.feature.gh-merge-base', 'main')
        args = parser().parse_args(['pr', 'create', '-R', shared.REPO, '--fill-first'])
        with patch('gitverse_cli.cli.Client', return_value=self.c):
            row = execute(args)
        self.assertEqual(row['head']['ref'], 'feature')
        self.assertEqual(row['fill']['mode'], 'first')

    def test_default_base_resolved_without_affecting_fill(self):
        args = parser().parse_args(['pr', 'create', '-R', shared.REPO, '--fill-first', '--head', 'feature'])
        with patch('gitverse_cli.cli.Client', return_value=self.c):
            row = execute(args)
        self.assertEqual(row['base']['ref'], 'main')
        self.assertEqual(row['fill']['mode'], 'first')

    def test_fill_modes_are_mutually_exclusive_before_network(self):
        for flag in ('--fill', '-f'):
            with self.assertRaises(GVError), patch('sys.stderr', io.StringIO()):
                self.call(flag)
        self.assertEqual(self.c.calls, [])

    def test_multi_commit_overrides_and_editor_seed_use_first_body(self):
        self.commit('Later title', 'Later body')
        for flags, title, body in [(['--title', 'Explicit'], 'Explicit', 'Body\n\nLiteral $HOME\n'),
                                   (['--body', ''], 'Subject 😀', ''),
                                   (['--title', 'Explicit', '--body', ''], 'Explicit', '')]:
            self.reset_client()
            row = self.call(*flags)
            self.assertEqual((row['title'], row['body']), (title, body))
        self.reset_client()
        with patch('gitverse_cli.comment_editor.edit_text', return_value='Edited\nFinal') as editor:
            self.call('--editor')
        self.assertEqual(editor.call_args.args[0], 'Subject 😀\nBody\n\nLiteral $HOME\n')

    def test_empty_first_commit_body_is_not_replaced_with_later_body(self):
        self.git('reset', '--hard', self.base)
        self.commit('First no body')
        self.commit('Second', 'Later body')
        self.reset_client()
        row = self.call()
        self.assertEqual((row['title'], row['body']), ('First no body', ''))

    def test_even_explicit_fields_require_complete_fill_history(self):
        self.git('update-ref', '-d', 'refs/remotes/origin/main')
        with self.assertRaises(GVError):
            self.call('--title', 'Explicit', '--body', 'Explicit')
        self.assertEqual(self.c.calls, [])
