import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.workflows import workflow_command


def row(file='test.yml', name='Test', state='active'):
    return {'workflow': file, 'name': name, 'state': state}


def inputs(*items): return {'inputs': list(items)}


def item(name='message', kind='string', required=True, **kwargs):
    return {'name': name, 'type': kind, 'required': required, **kwargs}


class WorkflowTests(unittest.TestCase):
    def call(self, replies, action='run', *flags):
        args = ['workflow', action, '-R', 'o/r']
        if action != 'list': args += ['test.yml']
        self.client = Mock(dry_run=False)
        self.client.request.side_effect = replies
        return workflow_command(self.client, 'o/r', parser().parse_args(args + list(flags)))

    def test_dispatch_exact_query_body_and_no_invented_run_id(self):
        result = self.call([row(), inputs(item()), None], 'run', '--ref', 'feature/a', '-f', 'message=Привет\n$HOME=$(literal)')
        self.assertTrue(result['accepted']); self.assertIsNone(result['run_id'])
        c = self.client.request.call_args
        self.assertEqual(c.args, ('POST', 'repos/o/r/actions/workflows/test.yml/dispatches', {'branch': 'feature/a'}))
        self.assertEqual(c.kwargs['body'], {'message': 'Привет\n$HOME=$(literal)'})

    def test_default_branch_is_read_not_guessed(self):
        result = self.call([row(), {'default_branch': 'develop'}, inputs(), None])
        self.assertEqual(result['target'], {'branch': 'develop'})

    def test_tag_is_explicit_separate_query(self):
        result = self.call([row(), inputs(), None], 'run', '--tag', 'v1')
        self.assertEqual(result['target'], {'tag': 'v1'})

    def test_view_metadata_does_not_require_dispatch_trigger(self):
        self.assertEqual(self.call([row()], 'view'), row())
        self.assertEqual(self.client.request.call_count, 1)

    def test_view_inputs_uses_target(self):
        result = self.call([row(), inputs()], 'view', '--inputs', '--tag', 'v2')
        self.assertEqual(result['dispatch'], inputs())
        self.assertEqual(result['target'], {'tag': 'v2'})

    def test_exact_name_resolves_after_full_capped_catalog(self):
        args = parser().parse_args(['workflow', 'run', 'Test', '-R', 'o/r', '--ref', 'main'])
        c = Mock(dry_run=False)
        c.request.side_effect = [{'workflows': [row('other.yml', 'Other')], 'total_count': 2}, {'workflows': [row()], 'total_count': 2}, row(), inputs(), None]
        self.assertTrue(workflow_command(c, 'o/r', args)['accepted'])

    def test_ambiguous_name_refuses_before_mutation(self):
        args = parser().parse_args(['workflow', 'run', 'Test', '-R', 'o/r'])
        c = Mock(dry_run=False); c.request.return_value = {'workflows': [row(), row('other.yml')], 'total_count': 2}
        with self.assertRaises(GVError) as error: workflow_command(c, 'o/r', args)
        self.assertEqual(error.exception.code, 'ambiguous_workflow')
        self.assertTrue(all(call.args[0] == 'GET' for call in c.request.call_args_list))

    def test_list_active_all_and_pagination_after_filtering(self):
        rows = [row('off.yml', state='disabled'), row(), row('last.yml')]
        data = {'workflows': rows, 'total_count': 3}
        self.assertEqual(self.call([data], 'list', '--limit', '1', '--page', '2'), [rows[2]])
        self.assertEqual(self.call([data], 'list', '--all', '--paginate', '--limit', '1'), rows)

    def test_list_without_total_reads_until_empty(self):
        self.assertEqual(self.call([{'workflows': [row()]}, {'workflows': []}], 'list'), [row()])

    def test_incomplete_repeated_shifted_or_bounded_pages_fail(self):
        for replies, flags in (([{'workflows': [row()], 'total_count': 2}, {'workflows': [], 'total_count': 2}], []),
                               ([{'workflows': [row()]}, {'workflows': [row()]}], []),
                               ([{'workflows': [row()], 'total_count': 2}, {'workflows': [], 'total_count': 3}], []),
                               ([{'workflows': [row()], 'total_count': 2}], ['--max-pages', '1'])):
            with self.assertRaises(GVError): self.call(replies, 'list', *flags)

    def test_invalid_inputs_stop_before_post(self):
        cases = [(inputs(item()), []), (inputs(), ['-f', 'unknown=x']),
                 (inputs(item(kind='boolean')), ['-f', 'message=yes']),
                 (inputs(item(kind='choice', options=['one'])), ['-f', 'message=two']),
                 (inputs(item(kind='number')), ['-f', 'message=nan']),
                 (inputs(item(kind='future')), ['-f', 'message=x'])]
        for meta, flags in cases:
            with self.assertRaises(GVError): self.call([row(), meta], 'run', '--ref', 'main', *flags)
            self.assertFalse(any(c.args[0] == 'POST' for c in self.client.request.call_args_list))

    def test_required_defaults_boolean_false_and_choices(self):
        meta = inputs(item(default='default'), item('flag', 'boolean'), item('choice', 'choice', options=['one']))
        result = self.call([row(), meta, None], 'run', '--ref', 'main', '-f', 'flag=false', '-f', 'choice=one')
        self.assertTrue(result['accepted'])

    def test_duplicate_or_invalid_field_is_local_error(self):
        for flags in (['-f', 'x=a', '-f', 'x=b'], ['-f', 'missing'], ['-f', '=empty']):
            with self.assertRaises(GVError): self.call([], 'run', *flags)
            self.client.request.assert_not_called()

    def test_json_file_stdin_strings_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'inputs.json'
            for text in ('{"message":true}', '[]', '{"message":"a","message":"b"}'):
                p.write_text(text)
                with self.assertRaises(GVError): self.call([], 'run', '--input', str(p))
            p.write_text('{"message":"Привет"}')
            self.assertTrue(self.call([row(), inputs(item()), None], 'run', '--ref', 'main', '--input', str(p))['accepted'])
        with patch('sys.stdin', io.StringIO('{"message":"stdin"}')):
            self.assertTrue(self.call([row(), inputs(item()), None], 'run', '--ref', 'main', '--input', '-')['accepted'])

    def test_inactive_unknown_identity_and_missing_metadata_refuse_mutation(self):
        for replies in ([row(state='disabled'), inputs()], [row('wrong.yml')], [row(), {}], [row(), inputs({'name': 'm'})]):
            with self.assertRaises(GVError): self.call(replies, 'run', '--ref', 'main')
            self.assertFalse(any(c.args[0] == 'POST' for c in self.client.request.call_args_list))

    def test_dispatch_network_error_records_uncertainty_and_never_retries(self):
        with self.assertRaises(GVError) as error:
            self.call([row(), inputs(), GVError('network', 'network_error')], 'run', '--ref', 'main')
        self.assertTrue(error.exception.details['mutation_attempted'])
        self.assertEqual(error.exception.details['stage'], 'dispatch')
        self.assertEqual(sum(c.args[0] == 'POST' for c in self.client.request.call_args_list), 1)

    def test_deadlines_and_unexpected_write_response(self):
        with self.assertRaises(GVError) as error: self.call([row(), inputs(), {'latest_run': 99}], 'run', '--ref', 'main')
        self.assertTrue(error.exception.details['mutation_attempted'])
        self.assertEqual(len({c.kwargs['deadline'] for c in self.client.request.call_args_list}), 1)

    def test_invalid_flags_web_backend_and_offline_plans(self):
        for flags in (['--ref', 'a', '--tag', 'b'], ['--input', '-', '-f', 'a=b'], ['--timeout', 'nan']):
            with self.assertRaises(GVError): parser().parse_args(['workflow', 'run', 'test.yml', *flags])
        with self.assertRaises(GVError): execute(parser().parse_args(['workflow', 'list', '-R', 'o/r', '--backend', 'web', '--dry-run']))
        for action in ('list', 'view', 'run'):
            args = ['workflow', action, '-R', 'o/r', '--dry-run']
            if action != 'list': args += ['Test']
            self.assertIn('plan', execute(parser().parse_args(args)))

    def test_filename_path_prefix_encoded_and_invalid_selectors(self):
        args = parser().parse_args(['workflow', 'view', '.gitverse/workflows/test.yml', '-R', 'o/r'])
        c = Mock(dry_run=False); c.request.return_value = row()
        self.assertEqual(workflow_command(c, 'o/r', args), row())
        for name in ('123', '../test.yml', 'https://example.com/test.yml'):
            args.workflow = name
            with self.assertRaises(GVError): workflow_command(c, 'o/r', args)

    def test_view_ref_without_inputs_is_not_silently_ignored(self):
        with self.assertRaises(GVError): self.call([], 'view', '--ref', 'main')
        self.client.request.assert_not_called()
