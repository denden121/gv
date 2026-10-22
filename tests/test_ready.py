import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch

from gitverse_cli.cli import execute, main, parser
from gitverse_cli.client import Client, GVError
from gitverse_cli.discovery import schema
from gitverse_cli.ready import pr_ready


def pull(title='Draft: Изменение `$HOME` $(literal)', draft=True, **extra):
    return {'number': 7, 'title': title, 'draft': draft, 'state': 'open', 'merged': False, **extra}


class ReadyTests(unittest.TestCase):
    def args(self, *flags):
        return parser().parse_args(['pr', 'ready', '7', '-R', 'owner/repo', *flags])

    def client(self, *responses):
        c = Mock(backend='public', dry_run=False)
        c.request.side_effect = responses
        return c

    def test_ready_changes_only_title_then_verifies(self):
        initial = pull()
        final = pull('Изменение `$HOME` $(literal)', False)
        c = self.client(initial, initial, None, final)
        result = pr_ready(self.args(), c, 'owner/repo', 7)
        self.assertTrue(result['changed'])
        self.assertFalse(result['draft'])
        calls = c.request.call_args_list
        self.assertEqual([call.args[0] for call in calls], ['GET', 'GET', 'PATCH', 'GET'])
        self.assertEqual(calls[2].kwargs, {'body': {'title': final['title']}})
        props = schema('/repos/{owner}/{repo}/pulls/{pull_number}', 'PATCH')['definitions']['#/definitions/request.UpdatePullRequestAPI']['properties']
        self.assertIn('title', props)
        self.assertNotIn('draft', props)

    def test_undo_adds_prefix_and_preserves_title_verbatim(self):
        initial = pull(' Изменение  ', False)
        final = pull('Draft:  Изменение  ', True)
        c = self.client(initial, initial, {}, final)
        result = pr_ready(self.args('--undo'), c, 'owner/repo', 7)
        self.assertEqual(result['title'], final['title'])
        self.assertTrue(result['draft'])

    def test_repeated_calls_do_not_mutate(self):
        for desired in (False, True):
            c = self.client(pull('Draft: Change' if desired else 'Change', desired))
            result = pr_ready(self.args(*(['--undo'] if desired else [])), c, 'owner/repo', 7)
            self.assertFalse(result['changed'])
            self.assertEqual(c.request.call_count, 1)

    def test_case_and_optional_space_follow_frontend_prefix(self):
        for title in ('draft: Change ', 'DRAFT : Change', 'DrAfT:\tChange'):
            initial = pull(title)
            c = self.client(initial, initial, None, pull('Change', False))
            self.assertEqual(pr_ready(self.args(), c, 'owner/repo', 7)['title'], 'Change')

    def test_empty_nested_and_unknown_prefixes_refuse_before_write(self):
        for title in ('Draft:', 'Draft:   ', 'Draft: Draft: Change', 'WIP: Change', '[Draft] Change'):
            c = self.client(pull(title))
            with self.assertRaises(GVError) as e:
                pr_ready(self.args(), c, 'owner/repo', 7)
            self.assertEqual(e.exception.code, 'unsupported_draft_title')
            self.assertEqual(c.request.call_count, 1)

    def test_inconsistent_non_draft_does_not_add_another_prefix(self):
        c = self.client(pull(draft=False))
        with self.assertRaises(GVError) as e:
            pr_ready(self.args('--undo'), c, 'owner/repo', 7)
        self.assertEqual(e.exception.code, 'unsupported_draft_title')

    def test_closed_merged_and_unknown_state_never_reopen(self):
        for extra in ({'state': 'closed'}, {'merged': True}, {'state': None}):
            for flag in ([], ['--undo']):
                c = self.client(pull(**extra))
                with self.assertRaises(GVError) as e:
                    pr_ready(self.args(*flag), c, 'owner/repo', 7)
                self.assertEqual(e.exception.code, 'pr_not_open')
                self.assertEqual(c.request.call_count, 1)

    def test_invalid_response_never_mutates(self):
        for response in (None, [], pull(number=8), pull(number=True), pull(draft=None), pull(draft=1), pull(title=None), pull(title=' ')):
            c = self.client(response)
            with self.assertRaises(GVError) as e:
                pr_ready(self.args(), c, 'owner/repo', 7)
            self.assertEqual(e.exception.code, 'unexpected_response')
            self.assertEqual(c.request.call_count, 1)

    def test_concurrent_change_before_patch_refuses_write(self):
        for latest in (pull('Draft: New title'), pull('Ready title', False)):
            c = self.client(pull(), latest)
            with self.assertRaises(GVError) as e:
                pr_ready(self.args(), c, 'owner/repo', 7)
            self.assertEqual(e.exception.code, 'pr_changed')
            self.assertEqual(c.request.call_count, 2)

    def test_closed_during_preflight_refuses_write(self):
        c = self.client(pull(), pull(state='closed'))
        with self.assertRaises(GVError):
            pr_ready(self.args(), c, 'owner/repo', 7)
        self.assertEqual(c.request.call_count, 2)

    def test_no_success_when_patch_is_ignored_or_title_differs(self):
        for final in (pull(), pull('Different', False), pull('Change', False, state='closed')):
            c = self.client(pull(), pull(), {}, final)
            with self.assertRaises(GVError) as e:
                pr_ready(self.args(), c, 'owner/repo', 7)
            self.assertTrue(e.exception.details['mutation_attempted'])
            self.assertEqual(e.exception.details['stage'], 'verify')
            self.assertEqual(c.request.call_count, 4)

    def test_mutation_and_verification_errors_preserve_recovery_context(self):
        for stage in ('update', 'verify'):
            responses = [pull(), pull()]
            if stage == 'verify':
                responses.append(None)
            responses.append(GVError('Connection failed', 'network_error'))
            c = self.client(*responses)
            with self.assertRaises(GVError) as e:
                pr_ready(self.args(), c, 'owner/repo', 7)
            self.assertEqual(e.exception.code, 'network_error')
            self.assertEqual(e.exception.details['stage'], stage)
            self.assertTrue(e.exception.details['mutation_attempted'])
            self.assertEqual(c.request.call_count, len(responses))

    def test_dry_run_is_offline_and_does_not_invent_current_title(self):
        args = self.args('--undo', '--dry-run')
        with patch.object(Client, 'request', wraps=None) as request:
            request.return_value = {'method': 'GET'}
            result = pr_ready(args, Client(dry_run=True), 'owner/repo', 7)
        self.assertTrue(result['requested_draft'])
        self.assertNotIn('body', result['mutation_plan'])
        request.assert_called_once_with('GET', 'repos/owner/repo/pulls/7')

    def test_web_backend_is_rejected_without_network(self):
        c = self.client(); c.backend = 'web'
        with self.assertRaises(GVError) as e:
            pr_ready(self.args(), c, 'owner/repo', 7)
        self.assertEqual(e.exception.code, 'backend_unsupported')
        c.request.assert_not_called()

    def test_url_selects_repository_and_conflicts_are_rejected(self):
        args = parser().parse_args(['pr', 'ready', 'https://gitverse.ru/owner/repo/pulls/7', '--undo', '--dry-run'])
        with patch.dict('os.environ', {}, clear=True):
            result = execute(args)
            self.assertEqual(result['repository'], 'owner/repo')
            args.repo = 'other/repo'
            with self.assertRaises(GVError):
                execute(args)

    def test_cli_json_fields_and_noop(self):
        with patch('gitverse_cli.cli.Client', return_value=self.client(pull())), contextlib.redirect_stdout(io.StringIO()) as output:
            main(['pr', 'ready', '7', '-R', 'owner/repo', '--undo', '--fields', 'number,draft,changed'])
        self.assertEqual(json.loads(output.getvalue()), {'number': 7, 'draft': True, 'changed': False})


if __name__ == '__main__':
    unittest.main()
