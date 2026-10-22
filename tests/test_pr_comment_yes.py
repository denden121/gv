"""Confirmation is authorization to delete once, never a safety override."""
import builtins
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.comment_delete import delete_last
from test_pr_comment_delete_last import DeleteFake


class CommentYesTests(unittest.TestCase):
    def args(self, *flags, selector='9'):
        args = parser().parse_args(['pr', 'comment', selector, '-R', 'o/r', *flags])
        args.number = 9
        return args

    def test_yes_deletes_once_without_prompt_or_text_input(self):
        client = DeleteFake()
        with patch.object(builtins, 'input', side_effect=AssertionError('prompt')):
            result = delete_last(client, 'o/r', self.args('--delete-last', '--yes'))
        self.assertTrue(result['changed'])
        self.assertEqual(result['comment_id'], 55)
        self.assertEqual([c[0] for c in client.calls if c[0] != 'GET'], ['DELETE'])
        self.assertEqual(client.calls[-1][1], 'repos/o/r/issues/comments/55')

    def test_yes_optional_for_identical_honest_offline_preview(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')), \
                patch.object(builtins, 'input', side_effect=AssertionError('prompt')):
            for selector in ('9', 'https://gitverse.ru/o/r/pulls/9', 'branch'):
                plain = execute(self.args('--delete-last', '--dry-run', selector=selector))
                yes = execute(self.args('--delete-last', '--yes', '--dry-run', selector=selector))
                self.assertEqual(plain, yes)
                if selector == 'branch':
                    self.assertIn('selection', yes)
                else:
                    self.assertTrue(yes['dry_run'])
                self.assertNotIn('comment_id', yes)

    def test_missing_yes_refuses_before_client_or_branch_lookup(self):
        with patch('gitverse_cli.cli.Client', side_effect=AssertionError('client')), \
                patch.object(builtins, 'input', side_effect=AssertionError('prompt')):
            with self.assertRaises(GVError) as caught:
                execute(self.args('--delete-last', selector='branch'))
        self.assertEqual(caught.exception.code, 'usage_error')

    def test_yes_requires_delete_and_rejects_other_modes_before_lookup(self):
        for flags in ((), ('--body', 'text'), ('--edit-last', '--body', 'text'),
                      ('--web',), ('--editor',), ('--reply-to', '1', '--body', 'text')):
            with self.subTest(flags=flags), \
                    patch('gitverse_cli.cli.Client', side_effect=AssertionError('client')):
                with self.assertRaises(GVError) as caught:
                    execute(self.args('--yes', *flags, selector='branch'))
                self.assertEqual(caught.exception.code, 'usage_error')

    def test_yes_is_not_absent_comment_noop_or_permission_override(self):
        for code, status in (('comment_not_found', None), ('http_error', 403)):
            client = DeleteFake()
            client.rows = []
            if status:
                def hook(c, method, path):
                    if path.endswith('/9/comments'):
                        raise GVError('denied', 'http_error', status)
                client.hook = hook
            with self.assertRaises(GVError) as caught:
                delete_last(client, 'o/r', self.args('--delete-last', '--yes'))
            self.assertEqual(caught.exception.code, code)
            self.assertFalse(any(c[0] != 'GET' for c in client.calls))

    def test_yes_does_not_override_wrong_scope_or_concurrency(self):
        for concurrent in (False, True):
            client = DeleteFake()
            if concurrent:
                def hook(c, method, path):
                    if '/issues/comments/' in path:
                        c.rows[0]['updated_at'] = 'changed'
                client.hook = hook
            else:
                client.rows[0]['issue_url'] = 'https://other.invalid/repos/o/r/issues/9'
            with self.assertRaises(GVError):
                delete_last(client, 'o/r', self.args('--delete-last', '--yes'))
            self.assertFalse(any(c[0] != 'GET' for c in client.calls))

    def test_yes_unknown_delete_keeps_recovery_id_and_never_repeats(self):
        client = DeleteFake()
        client.fail = OSError('synthetic transport failure')
        with self.assertRaises(GVError) as caught:
            delete_last(client, 'o/r', self.args('--delete-last', '--yes'))
        details = caught.exception.details
        self.assertEqual(details['comment_id'], 55)
        self.assertEqual(details['stage'], 'delete')
        self.assertTrue(details['mutation_attempted'])
        self.assertFalse(details['deletion_accepted'])
        self.assertEqual(sum(c[0] == 'DELETE' for c in client.calls), 1)

    def test_yes_does_not_hide_catalog_limit(self):
        client = DeleteFake()
        with self.assertRaises(GVError) as caught:
            delete_last(client, 'o/r', self.args('--delete-last', '--yes', '--max-entries', '1'))
        self.assertEqual(caught.exception.code, 'catalog_limit')
        self.assertFalse(any(c[0] != 'GET' for c in client.calls))

    def test_discovery_explains_confirmation_and_recovery(self):
        info = execute(parser().parse_args(['agent-info']))['discussion_comment_yes']
        self.assertEqual(info['requires'], '--delete-last')
        self.assertIn('never prompts', info['behavior'])
        self.assertIn('no network', info['dry_run'])
        self.assertIn('reconcile', info['recovery'])
