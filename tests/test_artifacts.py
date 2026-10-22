import copy
import hashlib
import io
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
import urllib.error
import zipfile
from unittest.mock import Mock, patch

from gitverse_cli.artifacts import download_artifacts
from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError

SHA = 'a' * 40


def artifact(identifier=1, name='report', **kwargs):
    return {'id': identifier, 'name': name, 'expired': False, 'size_in_bytes': 100,
            'workflow_run': {'id': 7, 'head_sha': SHA}, **kwargs}


def zip_bytes(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, body in files: archive.writestr(name, body)
    return stream.getvalue()


class FakeClient:
    dry_run = False
    def __init__(self, rows=None, archives=None):
        self.rows = rows if rows is not None else [artifact()]
        self.archives = archives or {r['id']: zip_bytes([('folder/Привет.txt', b'hello\n')]) for r in self.rows}
        self.calls = []; self.pages = None; self.after = None; self.dest_hook = None
    def request(self, method, path, query=None, deadline=None):
        self.calls.append((method, path, query))
        if path.endswith('/runs/7'): return {'id': 7, 'commit_sha': SHA}
        if path.endswith('/artifacts'):
            return copy.deepcopy(self.pages.get(query['page'], {'artifacts': []}) if self.pages is not None else {'artifacts': self.rows, 'total_count': len(self.rows)})
        identifier = int(path.split('/')[-1])
        row = next(r for r in self.rows if r['id'] == identifier)
        return copy.deepcopy(self.after or row)
    def download_to(self, path, output, max_bytes, deadline):
        identifier = int(path.split('/')[-3]); data = self.archives[identifier]
        if self.dest_hook: self.dest_hook()
        if len(data) > max_bytes: raise GVError('limit', 'download_limit')
        output.write(data)
        return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.dest = self.root / 'result'
    def call(self, client=None, *flags):
        args = parser().parse_args(['run', 'download', '7', '-R', 'o/r', '-D', str(self.dest), *flags])
        return download_artifacts(client or FakeClient(), 'o/r', args)
    def assert_clean(self):
        self.assertFalse(self.dest.exists())
        self.assertFalse(list(self.root.glob('.gv-download-*')))

    def test_extract_unicode_and_manifest_hash(self):
        client = FakeClient(); result = self.call(client)
        self.assertEqual((self.dest / 'folder/Привет.txt').read_bytes(), b'hello\n')
        self.assertEqual(result['artifacts'][0]['sha256'], hashlib.sha256(client.archives[1]).hexdigest())
        self.assertEqual(result['totals']['files'], 1)

    def test_archive_preserves_original_bytes(self):
        client = FakeClient(); result = self.call(client, '--archive')
        self.assertEqual((self.dest / '1.zip').read_bytes(), client.archives[1])
        self.assertEqual(result['mode'], 'archive')

    def test_multiple_artifacts_use_ids_and_exact_repeated_names(self):
        result = self.call(FakeClient([artifact(), artifact(2, 'second')]), '-n', 'report', '-n', 'second')
        self.assertEqual(len(result['artifacts']), 2)
        self.assertTrue((self.dest / '1/folder/Привет.txt').exists())
        self.assertTrue((self.dest / '2/folder/Привет.txt').exists())

    def test_missing_expired_wrong_sha_and_empty_refuse_before_publish(self):
        for client, flags in ((FakeClient([]), []), (FakeClient(), ['-n', 'absent']),
                              (FakeClient([artifact(expired=True)]), []),
                              (FakeClient([artifact(workflow_run={'id': 7, 'head_sha': 'b' * 40})]), []),
                              (FakeClient([artifact(workflow_run={'id': 8, 'head_sha': SHA})]), [])):
            with self.assertRaises(GVError): self.call(client, *flags)
            self.assert_clean()

    def test_existing_directory_file_and_symlink_are_preserved(self):
        self.dest.mkdir(); (self.dest / 'keep').write_text('safe')
        with self.assertRaises(GVError): self.call()
        self.assertEqual((self.dest / 'keep').read_text(), 'safe')
        target = self.root / 'target'; target.mkdir()
        self.dest = self.root / 'link'; self.dest.symlink_to(target, target_is_directory=True)
        with self.assertRaises(GVError): self.call()
        self.assertTrue(self.dest.is_symlink())

    def test_destination_appearing_mid_download_is_preserved(self):
        client = FakeClient()
        def hook(): self.dest.mkdir(); (self.dest / 'keep').write_text('safe')
        client.dest_hook = hook
        with self.assertRaises(GVError): self.call(client)
        self.assertEqual((self.dest / 'keep').read_text(), 'safe')
        self.assertFalse((self.dest / 'folder').exists())

    def test_path_traversal_absolute_windows_and_reserved_names(self):
        for name in ('../escape', '/tmp/escape', 'a/../../escape', 'C:/escape', 'a\\escape', 'a/./b', 'CON.txt', 'space /b', 'a\x00hidden'):
            # ZipInfo truncates NUL on creation, so test the path validator too.
            if '\x00' in name:
                from gitverse_cli.artifacts import portable_parts
                with self.assertRaises(GVError): portable_parts(name)
                continue
            with self.assertRaises(GVError): self.call(FakeClient(archives={1: zip_bytes([(name, b'x')])}))
            self.assert_clean()
        self.assertFalse((self.root / 'escape').exists())

    def test_symlink_and_special_files_refused(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR):
            entry = zipfile.ZipInfo('link'); entry.create_system = 3; entry.external_attr = (mode | 0o777) << 16
            with self.assertRaises(GVError): self.call(FakeClient(archives={1: zip_bytes([(entry, b'../escape')])}))
            self.assert_clean()

    def test_case_unicode_and_file_directory_collisions_refused(self):
        for names in (['A.txt', 'a.txt'], ['é.txt', 'e\u0301.txt'], ['file', 'file/child'], ['file/child', 'file'], ['A/x', 'a/y']):
            with self.assertRaises(GVError): self.call(FakeClient(archives={1: zip_bytes([(n, b'x') for n in names])}))
            self.assert_clean()

    def test_corrupt_zip_crc_and_encrypted_entries_refused(self):
        valid = zip_bytes([('x', b'data')])
        encrypted = bytearray(valid); encrypted[6] |= 1
        central = encrypted.find(b'PK\x01\x02'); encrypted[central + 8] |= 1
        corrupt = bytearray(valid); corrupt[31] ^= 255
        for data in (b'<html>login', valid[:-8], bytes(encrypted), bytes(corrupt)):
            with self.assertRaises(GVError): self.call(FakeClient(archives={1: data}))
            self.assert_clean()

    def test_compressed_expanded_and_entry_limits_are_global(self):
        for flags in (['--max-bytes', '1'], ['--max-extracted-bytes', '1'], ['--max-files', '1']):
            client = FakeClient([artifact(), artifact(2, 'second')])
            with self.assertRaises(GVError): self.call(client, *flags)
            self.assert_clean()

    def test_second_archive_failure_does_not_publish_first(self):
        client = FakeClient([artifact(), artifact(2, 'second')], {1: zip_bytes([('ok', b'x')]), 2: b'invalid'})
        with self.assertRaises(GVError): self.call(client)
        self.assert_clean()

    def test_shifting_artifact_identity_refuses_download(self):
        client = FakeClient(); client.after = artifact(name='changed')
        with self.assertRaises(GVError): self.call(client)
        self.assert_clean()

    def test_artifact_change_after_download_and_run_change_cleanup(self):
        for change in ('artifact', 'run'):
            client = FakeClient()
            if change == 'artifact': client.dest_hook = lambda: setattr(client, 'after', artifact(expired=True))
            else:
                original = client.request
                reads = []
                def request(method, path, query=None, deadline=None):
                    result = original(method, path, query, deadline)
                    if path.endswith('/runs/7'):
                        reads.append(1)
                        if len(reads) > 1: result['commit_sha'] = 'b' * 40
                    return result
                client.request = request
            with self.assertRaises(GVError): self.call(client)
            self.assert_clean()

    def test_forged_zip_directory_count_is_rejected_before_extraction(self):
        data = bytearray(zip_bytes([('file', b'hello')]))
        offset = data.rfind(b'PK\x05\x06')
        data[offset + 8:offset + 12] = b'\x00' * 4
        with self.assertRaises(GVError): self.call(FakeClient(archives={1: bytes(data)}))
        self.assert_clean()

    def test_pagination_caps_repeats_empty_count_and_bound(self):
        client = FakeClient([artifact(), artifact(2, 'second')])
        client.pages = {1: {'artifacts': [artifact()], 'total_count': 2}, 2: {'artifacts': [artifact(2, 'second')], 'total_count': 2}}
        self.assertEqual(len(self.call(client)['artifacts']), 2)
        self.dest = self.root / 'other'
        for second, flags in (({'artifacts': [artifact()], 'total_count': 2}, []), ({'artifacts': [], 'total_count': 2}, []),
                              ({'artifacts': [], 'total_count': 3}, []), ({'artifacts': [], 'total_count': 2}, ['--max-pages', '1'])):
            client.pages[2] = second
            with self.assertRaises(GVError): self.call(client, *flags)
            self.assert_clean()

    def test_deadline_cleanup_and_dry_run_no_writes(self):
        client = FakeClient()
        with patch('gitverse_cli.artifacts.time.monotonic', side_effect=[0, 1000]):
            with self.assertRaises(GVError): self.call(client, '--timeout', '1')
        self.assert_clean()
        result = execute(parser().parse_args(['run', 'download', '7', '-R', 'o/r', '-D', str(self.dest), '--dry-run']))
        self.assertIn('plan', result); self.assert_clean()

    def test_invalid_flags_and_web_backend(self):
        for flags in (['--max-files', '0'], ['--max-bytes', '-1'], ['--timeout', 'inf']):
            with self.assertRaises(GVError): parser().parse_args(['run', 'download', '7', *flags])
        with self.assertRaises(GVError): execute(parser().parse_args(['run', 'download', '7', '-R', 'o/r', '--backend', 'web', '--dry-run']))


class Response(io.BytesIO):
    def __init__(self, body, length=None):
        super().__init__(body); self.headers = {} if length is None else {'Content-Length': length}


class BinaryTransportTests(unittest.TestCase):
    def client(self):
        c = Client(dry_run=True); c.dry_run = False; c.headers['Authorization'] = 'Bearer fake'; c.opener = Mock()
        return c
    def test_stream_preserves_bytes_hash_and_versioned_accept(self):
        c = self.client(); c.opener.open.return_value = Response(b'\x00\xffZIP', '5'); output = io.BytesIO()
        result = c.download_to('repos/o/r/actions/artifacts/1/zip/raw', output, 20, time.monotonic() + 10)
        self.assertEqual(output.getvalue(), b'\x00\xffZIP')
        self.assertEqual(result['sha256'], hashlib.sha256(output.getvalue()).hexdigest())
        request = c.opener.open.call_args.args[0]
        self.assertIn('version=1', request.get_header('Accept'))
    def test_size_limits_with_and_without_length(self):
        for length in ('100', None):
            c = self.client(); c.opener.open.return_value = Response(b'too large', length)
            with self.assertRaises(GVError): c.download_to('p', io.BytesIO(), 2, time.monotonic() + 10)
    def test_truncated_length_is_error(self):
        c = self.client(); c.opener.open.return_value = Response(b'abc', '10')
        with self.assertRaises(GVError) as error: c.download_to('p', io.BytesIO(), 20, time.monotonic() + 10)
        self.assertEqual(error.exception.code, 'incomplete_download')
    def test_redirect_refused_and_not_retried(self):
        c = self.client(); c.opener.open.side_effect = urllib.error.HTTPError('https://api.gitverse.ru/p', 302, 'redirect', {'Location': 'https://signed.example/secret'}, None)
        with self.assertRaises(GVError) as error: c.download_to('p', io.BytesIO(), 20, time.monotonic() + 10)
        self.assertEqual(error.exception.status, 302); self.assertEqual(c.opener.open.call_count, 1)
        self.assertNotIn('signed.example', str(error.exception))
    def test_deadline_before_request_and_external_path_refused(self):
        c = self.client()
        for path, deadline in (('p', 0), ('https://evil.example/file', time.monotonic() + 10)):
            with self.assertRaises(GVError): c.download_to(path, io.BytesIO(), 20, deadline)
        c.opener.open.assert_not_called()
