import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from gitverse_cli.cli import execute, main, parser
from gitverse_cli.client import Client, GVError
from gitverse_cli.contents import RawContents, read_contents

SHA = 'a' * 40


def blob(data, algorithm='sha1'):
    return hashlib.new(algorithm, b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def file_entry(data, path='folder/Привет.txt', **extra):
    return {'path': path, 'type': 'blob', 'mode': '100644', 'size': len(data), 'sha': blob(data), **extra}


class FakeClient:
    dry_run = False
    def __init__(self, data=b'hello\r\n$HOME $(literal)  '):
        self.data = data; self.calls = []; self.tree_pages = None; self.content_changes = {}
        self.entry = file_entry(data)
        self.rows = [{'path': 'folder', 'type': 'tree', 'mode': '040000', 'size': 0, 'sha': 'b' * 40}, self.entry]
    def request(self, method, path, query=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(query), kwargs))
        if path == 'repos/o/r': return {'default_branch': 'develop'}
        if '/commits/' in path: return {'sha': SHA}
        if path.endswith('/branches'): return [{'name': 'feature/a', 'commit': {'sha': SHA}}] if query['page'] == 1 else []
        if '/git/trees/' in path:
            if self.tree_pages: return copy.deepcopy(self.tree_pages[query['page']])
            return {'sha': SHA, 'page': query['page'], 'tree': copy.deepcopy(self.rows), 'total_count': len(self.rows), 'truncated': False}
        if '/contents/' in path:
            return {'path': self.entry['path'], 'type': 'file', 'sha': self.entry['sha'], 'size': len(self.data),
                    'encoding': 'base64', 'content': base64.b64encode(self.data).decode(), **self.content_changes}
        raise AssertionError(path)


class ContentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.out = self.root / 'out.bin'
    def call(self, client=None, action='read-file', path='folder/Привет.txt', *flags):
        args = parser().parse_args(['repo', action, path, '-R', 'o/r', *flags])
        self.client = client or FakeClient()
        return read_contents(self.client, 'o/r', args)

    def test_utf8_crlf_spaces_and_no_final_newline_preserved(self):
        data = 'Привет\r\n$HOME $(literal)  '.encode()
        result = self.call(FakeClient(data))
        self.assertEqual(result['content'].encode(), data)
        self.assertEqual(result['encoding'], 'utf-8'); self.assertFalse(result['binary'])
        self.assertEqual(result['commit_sha'], SHA)
        content_call = self.client.calls[-1]
        self.assertEqual(content_call[2]['ref'], SHA)
        self.assertIn('%D0%9F', content_call[1])
        self.assertTrue(all(c[3]['require_complete'] for c in self.client.calls))

    def test_binary_json_is_base64_and_empty_file_is_empty_text(self):
        data = b'\x00\xff\r\n'
        result = self.call(FakeClient(data))
        self.assertTrue(result['binary']); self.assertEqual(base64.b64decode(result['content']), data)
        self.assertEqual(self.call(FakeClient(b''))['content'], '')
        empty = FakeClient(b''); empty.content_changes = {'content': None}
        self.assertEqual(self.call(empty)['content'], '')

    def test_missing_content_only_accepted_for_verified_empty_blob(self):
        for data in (b'', b'not empty'):
            client = FakeClient(data)
            original = client.request
            def request(*args, **kwargs):
                result = original(*args, **kwargs)
                if '/contents/' in args[1]: result.pop('content')
                return result
            client.request = request
            if data:
                with self.assertRaises(GVError): self.call(client)
            else:
                self.assertEqual(self.call(client)['content'], '')

    def test_raw_bytes_and_output_exactness(self):
        data = b'\x00\xff\r\nwithout newline'
        raw = self.call(FakeClient(data), 'read-file', 'folder/Привет.txt', '--raw')
        self.assertIsInstance(raw, RawContents); self.assertEqual(raw.data, data)
        result = self.call(FakeClient(data), 'read-file', 'folder/Привет.txt', '--output', str(self.out))
        self.assertEqual(self.out.read_bytes(), data); self.assertEqual(result['bytes_written'], len(data))
        self.assertNotIn('content', result)

    def test_existing_output_and_symlink_refused_before_api(self):
        self.out.write_bytes(b'keep')
        client = FakeClient()
        with self.assertRaises(GVError): self.call(client, 'read-file', 'folder/Привет.txt', '-o', str(self.out))
        self.assertEqual(client.calls, []); self.assertEqual(self.out.read_bytes(), b'keep')
        link = self.root / 'link'; link.symlink_to(self.out)
        with self.assertRaises(GVError): self.call(client, 'read-file', 'folder/Привет.txt', '-o', str(link))
        self.assertEqual(self.out.read_bytes(), b'keep')

    def test_output_appearing_during_write_is_not_overwritten(self):
        original = os.link
        def race(source, target):
            Path(target).write_bytes(b'concurrent user data')
            return original(source, target)
        with patch('gitverse_cli.contents.os.link', side_effect=race):
            with self.assertRaises(GVError): self.call(None, 'read-file', 'folder/Привет.txt', '-o', str(self.out))
        self.assertEqual(self.out.read_bytes(), b'concurrent user data')
        self.assertFalse(list(self.root.glob('.gv-file-*')))

    def test_deadline_during_tree_and_output_preserves_error(self):
        client = FakeClient()
        with patch.object(client, 'request', side_effect=GVError('deadline', 'deadline_exceeded')):
            with self.assertRaises(GVError) as error: self.call(client)
        self.assertEqual(error.exception.code, 'deadline_exceeded')

    def test_directory_auto_contents_and_recursive_filter(self):
        result = self.call(None, 'read-dir', '')
        self.assertEqual([r['path'] for r in result['entries']], ['folder'])
        recursive = self.call(None, 'contents', '', '--recursive')
        self.assertEqual(len(recursive['entries']), 2)
        folder = self.call(None, 'read-dir', 'folder')
        self.assertEqual(folder['entries'][0]['path'], 'folder/Привет.txt')
        self.assertTrue(folder['coverage']['complete'])

    def test_sha_pinning_skips_resolution_but_tags_and_branches_resolve(self):
        self.call(None, 'read-file', 'folder/Привет.txt', '--ref', SHA)
        self.assertFalse(any('/commits/' in c[1] for c in self.client.calls))
        self.call(None, 'read-file', 'folder/Привет.txt', '--ref', 'feature/a')
        self.assertEqual(self.client.calls[0][1], 'repos/o/r/branches')
        self.assertEqual(self.client.calls[1][2]['page'], 2)

    def test_sha256_git_blob_verification(self):
        client = FakeClient(); client.entry['sha'] = blob(client.data, 'sha256')
        self.assertEqual(len(self.call(client)['sha']), 64)

    def test_slash_branch_lookup_refuses_incomplete_or_invalid_catalog(self):
        for values, flags, code in (([{'name': 'feature/a', 'commit': {'sha': SHA}}], [], 'pagination_error'),
                                    ([{'name': 'feature/a', 'commit': 'invalid'}], [], 'unexpected_response'),
                                    ([], [], 'ref_not_found'),
                                    ([{'name': 'feature/a', 'commit': {'sha': SHA}}], ['--max-pages', '1'], 'pagination_limit')):
            client = FakeClient()
            original = client.request
            def request(*args, **kwargs):
                if args[1].endswith('/branches'): return copy.deepcopy(values)
                return original(*args, **kwargs)
            client.request = request
            with self.assertRaises(GVError) as error:
                self.call(client, 'read-file', 'folder/Привет.txt', '--ref', 'feature/a', *flags)
            self.assertEqual(error.exception.code, code)

    def test_wrong_type_and_missing_paths_refused(self):
        for action, path in (('read-file', 'folder'), ('read-dir', 'folder/Привет.txt'), ('read-file', 'missing')):
            with self.assertRaises(GVError): self.call(None, action, path)

    def test_symlink_and_submodule_metadata_not_followed(self):
        for mode, kind in (('120000', 'blob'), ('160000', 'commit')):
            client = FakeClient(); client.entry.update(mode=mode, type=kind)
            result = self.call(client, 'contents')
            self.assertIn(result['type'], ('symlink', 'submodule'))
            self.assertFalse(any('/contents/' in c[1] for c in client.calls))
            with self.assertRaises(GVError): self.call(client)

    def test_blob_mismatch_base64_missing_content_and_size_refuse_output(self):
        for changes in ({'content': '!!!'}, {'content': None}, {'content': base64.b64encode(b'x' * 25).decode()}, {'size': 1}, {'sha': None}, {'encoding': 'none'}):
            client = FakeClient(); client.content_changes = changes
            with self.assertRaises(GVError): self.call(client, 'read-file', 'folder/Привет.txt', '-o', str(self.out))
            self.assertFalse(self.out.exists()); self.assertFalse(list(self.root.glob('.gv-file-*')))

    def test_full_tree_cap_paging_and_markers(self):
        client = FakeClient()
        client.tree_pages = {1: {'sha': SHA, 'page': 1, 'tree': [client.rows[0]], 'total_count': 2, 'truncated': True},
                             2: {'sha': SHA, 'page': 2, 'tree': [client.rows[1]], 'total_count': 2, 'truncated': False}}
        self.assertEqual(self.call(client)['size'], len(client.data))
        for replacement, flags in (({'sha': SHA, 'page': 2, 'tree': [], 'total_count': 2, 'truncated': False}, []),
                                   ({'sha': SHA, 'page': 2, 'tree': [client.rows[0]], 'total_count': 2, 'truncated': False}, []),
                                   (client.tree_pages[2], ['--max-pages', '1']),
                                   ({'sha': SHA, 'page': 2, 'tree': [client.rows[1]], 'total_count': 2, 'truncated': True}, [])):
            client.tree_pages[2] = replacement
            with self.assertRaises(GVError): self.call(client, 'read-file', 'folder/Привет.txt', *flags)

    def test_entry_and_file_limits_refuse_before_content_get(self):
        for flags in (['--max-entries', '1'], ['--max-bytes', '1']):
            with self.assertRaises(GVError): self.call(None, 'read-file', 'folder/Привет.txt', *flags)
            self.assertFalse(any('/contents/' in c[1] for c in self.client.calls))

    def test_tree_identity_parent_mode_and_count_are_checked(self):
        for changes in ('missing-parent', 'invalid-mode', 'missing-sha'):
            client = FakeClient()
            if changes == 'missing-parent': client.rows = [client.entry]
            elif changes == 'invalid-mode': client.entry['mode'] = '999999'
            else: client.entry['sha'] = None
            with self.assertRaises(GVError): self.call(client)

    def test_ref_path_flags_and_backend_validation(self):
        for path in ('../file', '/absolute', 'folder//file', 'folder/./file', 'a\\b'):
            with self.assertRaises(GVError): self.call(None, 'read-file', path)
        for flags in (['--page-size', '101'], ['--ref', ''], ['--raw', '--fields', 'path'], ['--output', '-']):
            with self.assertRaises(GVError): self.call(None, 'read-file', 'folder/Привет.txt', *flags)
        with self.assertRaises(GVError): self.call(None, 'contents', 'folder/Привет.txt', '--recursive')
        with self.assertRaises(GVError): execute(parser().parse_args(['repo', 'read-dir', '-R', 'o/r', '--backend', 'web', '--dry-run']))

    def test_dry_run_no_file_or_network_and_preserves_unresolved_ref(self):
        for action, path in (('contents', ''), ('read-dir', ''), ('read-file', 'hello')):
            args = ['repo', action, path, '-R', 'o/r', '--dry-run']
            if action == 'read-file': args += ['-o', str(self.out)]
            self.assertIn('plan', execute(parser().parse_args(args)))
        self.assertFalse(self.out.exists())

    def test_main_raw_stdout_is_binary_and_does_not_add_newline(self):
        capture = Mock(); capture.buffer = io.BytesIO()
        with patch('gitverse_cli.cli.execute', return_value=RawContents(b'\x00\xfflast')), patch('sys.stdout', capture):
            main(['repo', 'read-file', 'x', '-R', 'o/r', '--raw'])
        self.assertEqual(capture.buffer.getvalue(), b'\x00\xfflast')
        capture.write.assert_not_called()


class Response(io.BytesIO):
    def __init__(self, data, status=200): super().__init__(data); self.status = status


class CompleteResponseTests(unittest.TestCase):
    def test_partial_http_response_refused(self):
        c = Client(dry_run=True); c.dry_run = False; c.opener = Mock()
        c.opener.open.return_value = Response(b'{}', 206)
        with self.assertRaises(GVError) as error: c.request('GET', 'path', require_complete=True, max_response_bytes=10)
        self.assertEqual(error.exception.code, 'incomplete_response')

    def test_response_cap_and_invalid_utf8_refused(self):
        for data, limit in ((b'{"huge":"xxxxxxxx"}', 5), (b'\xff', 10)):
            c = Client(dry_run=True); c.dry_run = False; c.opener = Mock(); c.opener.open.return_value = Response(data)
            with self.assertRaises(GVError): c.request('GET', 'path', require_complete=True, max_response_bytes=limit)
