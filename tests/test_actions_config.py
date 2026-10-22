import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from gitverse_cli.actions_config import catalog, config_command, config_scope, read_value
from gitverse_cli.cli import execute, main, parser
from gitverse_cli.client import GVError
from gitverse_cli.issues import Session

VALUE = 'sensitive fixture\r\nПривет\n$HOME $(literal)'


def record(name='DEMO', kind='secret', value=VALUE):
    result = {'name': name, 'created_at': '2026-10-19T00:00:00Z'}
    if kind == 'variable': result.update(value=value, updated_at='2026-10-19T00:00:00Z')
    return result


class Fake:
    dry_run = False
    backend = 'public'
    def __init__(self):
        self.calls = []; self.secrets = {}; self.variables = {}; self.fail_verify = False; self.mutated = False; self.ignore = False; self.deny = False
    def request(self, method, path, query=None, body=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(query), copy.deepcopy(body)))
        if self.deny: raise GVError('backend reflected ' + VALUE, 'http_error', 403, {'query': VALUE})
        if self.fail_verify and self.mutated and method == 'GET': raise GVError('echo ' + VALUE, 'network_error', details={'secret': VALUE})
        kind = 'secret' if '/secrets' in path else 'variable'; key = kind + 's'; table = getattr(self, key)
        if method == 'GET':
            if path.endswith('/' + key):
                rows = list(table.values()) if query['page'] == 1 else []
                return {'total_count': len(table), key: copy.deepcopy(rows)}
            if path.split('/')[-1] not in table: raise GVError('missing', 'http_error', 404)
            return copy.deepcopy(table[path.split('/')[-1]])
        self.mutated = True
        name = body['name'] if body else path.split('/')[-1]
        if method == 'DELETE':
            if not self.ignore: del table[name]
            return None
        if not self.ignore:
            table[name] = record(name, kind, body['value'] if body else query['value'])
            if kind == 'secret': table[name]['value'] = query['value']  # Simulate an unexpected echoed field: must never escape CLI.
        return copy.deepcopy(table.get(name))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.client = Fake(); self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.file = self.root / 'value'; self.file.write_bytes(VALUE.encode())
    def call(self, kind, *arguments, stdin=None):
        args = parser().parse_args([kind, *arguments])
        with patch('sys.stdin', io.StringIO(VALUE if stdin is None else stdin)):
            return config_command(self.client, 'repos/o/r', args)
    def writes(self): return [r for r in self.client.calls if r[0] != 'GET']

    def test_secret_set_metadata_only_and_query_contract(self):
        result = self.call('secret', 'set', 'DEMO')
        self.assertTrue(result['write_accepted']); self.assertTrue(result['metadata_verified']); self.assertFalse(result['value_verified'])
        self.assertEqual(set(result['secret']), {'name', 'created_at'})
        self.assertNotIn(VALUE, json.dumps(result, ensure_ascii=False))
        self.assertEqual(self.writes()[0][:3], ('PUT', 'repos/o/r/actions/secrets/DEMO', {'value': VALUE}))

    def test_secret_update_always_puts_not_false_noop(self):
        self.client.secrets['DEMO'] = record()
        self.call('secret', 'set', 'DEMO'); self.call('secret', 'set', 'DEMO')
        self.assertEqual([r[0] for r in self.writes()], ['PUT', 'PUT'])

    def test_secret_get_list_drop_unexpected_value_and_extra_fields(self):
        self.client.secrets['DEMO'] = {**record(), 'value': VALUE, 'encoded_value': VALUE, 'debug': VALUE}
        for result in (self.call('secret', 'get', 'DEMO'), self.call('secret', 'list', '--paginate')):
            self.assertNotIn(VALUE, json.dumps(result, ensure_ascii=False))

    def test_secret_dry_run_does_not_read_input_file_or_emit_value(self):
        self.client.dry_run = True
        result = self.call('secret', 'set', 'DEMO', '--value-file', '/non-existing/value')
        self.assertEqual(result['value'], '<redacted>'); self.assertEqual(self.client.calls, [])

    def test_variable_create_update_noop_and_value_get(self):
        self.assertTrue(self.call('variable', 'set', 'DEMO')['value_verified'])
        self.assertEqual(self.writes()[0][0], 'POST')
        self.assertEqual(self.call('variable', 'get', 'DEMO')['value'], VALUE)
        self.assertFalse(self.call('variable', 'set', 'DEMO')['changed'])
        self.call('variable', 'set', 'DEMO', stdin='changed')
        self.assertEqual([r[0] for r in self.writes()], ['POST', 'PATCH'])
        self.assertNotIn('value', self.call('variable', 'list')['variables'][0])
        self.assertEqual(self.call('variable', 'list', '--show-values')['variables'][0]['value'], 'changed')

    def test_file_bytes_crlf_spaces_and_no_final_lf_preserved(self):
        self.call('variable', 'set', 'DEMO', '--value-file', str(self.file))
        self.assertEqual(self.writes()[0][3]['value'].encode(), self.file.read_bytes())
        self.assertNotIn(VALUE, json.dumps(self.call('variable', 'set', 'DEMO'), ensure_ascii=False))

    def test_empty_variable_allowed_but_empty_secret_refused(self):
        self.call('variable', 'set', 'DEMO', stdin='')
        self.assertEqual(self.client.variables['DEMO']['value'], '')
        before = len(self.writes())
        with self.assertRaises(GVError): self.call('secret', 'set', 'EMPTY', stdin='')
        self.assertEqual(len(self.writes()), before)

    def test_clear_nonempty_variable_refused_before_patch(self):
        self.client.variables['DEMO'] = record(kind='variable')
        with self.assertRaises(GVError) as error: self.call('variable', 'set', 'DEMO', stdin='')
        self.assertEqual(error.exception.code, 'backend_unsupported'); self.assertEqual(self.writes(), [])

    def test_delete_and_missing_delete_noop(self):
        for kind in ('secret', 'variable'):
            getattr(self.client, kind + 's')['DEMO'] = record(kind=kind)
            self.assertTrue(self.call(kind, 'delete', 'DEMO')['deleted'])
            self.assertFalse(self.call(kind, 'delete', 'DEMO')['changed'])

    def test_missing_get_and_case_alias_refused(self):
        with self.assertRaises(GVError) as error: self.call('variable', 'get', 'MISSING')
        self.assertEqual(error.exception.code, 'config_not_found')
        self.client.variables['demo'] = record('demo', 'variable')
        with self.assertRaises(GVError) as error: self.call('variable', 'set', 'DEMO')
        self.assertEqual(error.exception.code, 'ambiguous_name'); self.assertEqual(self.writes(), [])

    def test_value_limits_utf8_nul_symlink_and_fifo_guards(self):
        for arguments, stdin in ((('--max-bytes', '1'), VALUE), ((), '\0')):
            with self.assertRaises(GVError): self.call('secret', 'set', 'DEMO', *arguments, stdin=stdin)
        self.file.write_bytes(b'\xff')
        with self.assertRaises(GVError): self.call('secret', 'set', 'DEMO', '--value-file', str(self.file))
        link = self.root / 'link'; link.symlink_to(self.file)
        fifo = self.root / 'fifo'; os.mkfifo(fifo)
        for path in (link, fifo):
            with self.assertRaises(GVError): self.call('secret', 'set', 'DEMO', '--value-file', str(path))
        self.assertEqual(self.writes(), [])

    def test_invalid_name_flags_do_not_read_or_write(self):
        for name in ('lowercase', '1NAME', '../BAD', 'HAS SPACE', 'BAD\nNAME'):
            with self.assertRaises(GVError): self.call('secret', 'set', name)
        self.assertEqual(self.client.calls, [])

    def test_tty_input_refused(self):
        args = parser().parse_args(['secret', 'set', 'DEMO'])
        stdin = Mock(); stdin.isatty.return_value = True
        with patch('sys.stdin', stdin):
            with self.assertRaises(GVError): config_command(self.client, 'repos/o/r', args)
        stdin.read.assert_not_called(); self.assertEqual(self.writes(), [])

    def test_pipe_read_preserves_bytes_and_idle_stdin_deadline(self):
        import time
        reader, writer = os.pipe()
        os.write(writer, VALUE.encode()); os.close(writer)
        with os.fdopen(reader, 'rb') as source, patch('sys.stdin') as stdin:
            stdin.isatty.return_value = False; stdin.buffer = source
            args = parser().parse_args(['secret', 'set', 'DEMO'])
            self.assertEqual(read_value(args, time.monotonic() + 1), VALUE)
        reader, writer = os.pipe()
        try:
            with os.fdopen(reader, 'rb') as source, patch('sys.stdin') as stdin:
                stdin.isatty.return_value = False; stdin.buffer = source
                with self.assertRaises(GVError) as error: read_value(args, time.monotonic() + 0.01)
                self.assertEqual(error.exception.code, 'deadline_exceeded')
        finally: os.close(writer)

    def test_org_scope_conflicts_and_public_backend(self):
        args = parser().parse_args(['secret', 'list', '--org', 'my-org'])
        self.assertEqual(config_scope(args), 'orgs/my-org')
        args = parser().parse_args(['variable', 'list', '--org', 'my-org', '-R', 'o/r'])
        with self.assertRaises(GVError): config_scope(args)
        with self.assertRaises(GVError): execute(parser().parse_args(['secret', 'list', '-R', 'o/r', '--backend', 'web', '--dry-run']))

    def test_org_mutation_routes_match_public_contract(self):
        args = parser().parse_args(['variable', 'set', 'DEMO', '--org', 'my-org'])
        with patch('sys.stdin', io.StringIO(VALUE)): config_command(self.client, config_scope(args), args)
        self.assertEqual(self.writes()[0][1], 'orgs/my-org/actions/variables')

    def test_catalog_count_repeats_and_limits_do_not_return_partial(self):
        for scenario in ('repeat', 'short', 'changed-total', 'invalid-count', 'bound'):
            client = Mock()
            first = {'total_count': 2, 'secrets': [record()]}
            second = {'total_count': 2, 'secrets': [record()]}
            if scenario == 'short': second['secrets'] = []
            if scenario == 'changed-total': second['total_count'] = 3
            if scenario == 'invalid-count': first['total_count'] = True
            client.request.side_effect = [first, second]
            args = parser().parse_args(['secret', 'list', '--max-pages', '1' if scenario == 'bound' else '2'])
            with self.assertRaises(GVError): catalog(Session(client, args), 'p', 'secret')

    def test_concurrent_variable_preflight_refuses_patch(self):
        self.client.variables['DEMO'] = record(kind='variable')
        original = self.client.request; reads = 0
        def changed(method, path, *args, **kwargs):
            nonlocal reads
            if path.endswith('/DEMO'):
                reads += 1
                if reads == 2: self.client.variables['DEMO']['value'] = 'concurrent'
            return original(method, path, *args, **kwargs)
        self.client.request = changed
        with self.assertRaises(GVError) as error: self.call('variable', 'set', 'DEMO', stdin='new')
        self.assertEqual(error.exception.code, 'concurrent_change'); self.assertEqual(self.writes(), [])

    def test_ignored_variable_write_and_delete_are_not_success(self):
        self.client.ignore = True
        with self.assertRaises(GVError): self.call('variable', 'set', 'DEMO')
        self.client.variables['DEMO'] = record(kind='variable')
        with self.assertRaises(GVError): self.call('variable', 'delete', 'DEMO')

    def test_partial_write_errors_keep_context_but_never_reflect_value(self):
        self.client.fail_verify = True
        with self.assertRaises(GVError) as error: self.call('secret', 'set', 'DEMO')
        result = error.exception.as_dict()
        self.assertNotIn(VALUE, json.dumps(result, ensure_ascii=False))
        self.assertEqual(result['error']['details']['stage'], 'verify')
        self.assertTrue(result['error']['details']['mutation_attempted']); self.assertEqual(len(self.writes()), 1)

    def test_main_stderr_redacts_reflected_server_value(self):
        self.client.deny = True; stdout, stderr = io.StringIO(), io.StringIO()
        with patch('gitverse_cli.cli.Client', return_value=self.client), patch('sys.stdin', io.StringIO(VALUE)), patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            with self.assertRaises(SystemExit): main(['secret', 'set', 'DEMO', '-R', 'o/r'])
        self.assertNotIn(VALUE, stdout.getvalue() + stderr.getvalue()); self.assertNotIn('sensitive fixture', stderr.getvalue())

    def test_invalid_argv_does_not_echo_accidentally_supplied_value(self):
        for arguments in (['secret', 'set', 'DEMO', '--body', VALUE], ['secret', 'set', 'DEMO', '--max-bytes', VALUE], ['--backend', VALUE, 'secret', 'list']):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch('sys.stdout', stdout), patch('sys.stderr', stderr):
                with self.assertRaises(SystemExit): main(arguments)
            self.assertNotIn('sensitive fixture', stdout.getvalue() + stderr.getvalue())
