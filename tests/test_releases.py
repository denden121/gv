import copy
import hashlib
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from gitverse_cli.releases import release_command

SHA = 'a' * 40
UUID = '0ad10e5d-1b93-4c64-9e80-54264081bba4'


def release(identity=1, tag='v1', **extra):
    return {'id': identity, 'tag_name': tag, 'name': tag, 'body': '', 'draft': True, 'prerelease': False,
            'is_authorized_only': False, 'target_commitish': SHA, **extra}


def asset(name='test.txt', data=b'hello\r\n', identity=5):
    return {'id': identity, 'name': name, 'size': len(data), 'uuid': UUID, 'browser_download_url': 'https://evil.example/secret'}


class Fake:
    dry_run = False
    backend = 'public'
    def __init__(self):
        self.calls = []; self.rows = {1: release()}; self.data = b'hello\r\n'; self.files = [asset(data=self.data)]
        self.mutated = False; self.fail_verify = False; self.ignore = False; self.deny = False
    def request(self, method, path, query=None, body=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(query), copy.deepcopy(body), kwargs))
        if method == 'GET' and self.mutated and self.fail_verify: raise GVError('network failed', 'network_error')
        if method != 'GET':
            if self.deny: raise GVError('forbidden', 'http_error', 403)
            self.mutated = True
        if path == 'repos/o/r': return {'id': 1}
        if '/commits/' in path: return {'sha': SHA}
        if path.endswith('/releases'):
            if method == 'GET':
                if query['page'] == 1 and self.rows: return copy.deepcopy(list(self.rows.values()))
                raise GVError('empty page', 'http_error', 404)
            row = release(2, body['tag_name'], **{k: v for k, v in body.items() if k != 'tag_name'})
            self.rows[2] = row; return copy.deepcopy(row)
        if '/assets/' in path:
            identity = int(path.split('/')[-1])
            if method == 'DELETE' and not self.ignore: self.files = [r for r in self.files if r['id'] != identity]
            return None
        if path.endswith('/assets'):
            if method == 'GET': return copy.deepcopy(self.files if query['page'] == 1 else [])
            wire = kwargs['encoded_body']; self.data = wire.split(b'\r\n\r\n', 1)[1].rsplit(b'\r\n--', 1)[0]
            row = asset(query['name'], self.data, 6); self.files.append(row); return copy.deepcopy(row)
        if '/releases/' in path:
            identity = int(path.split('/')[-1])
            if identity not in self.rows: raise GVError('not found', 'http_error', 404)
            row = self.rows[identity]
            if method == 'DELETE':
                if not self.ignore: del self.rows[identity]
                return None
            if method == 'PATCH' and not self.ignore: row.update(body)
            return copy.deepcopy(row)
        raise AssertionError((method, path))
    def download_to(self, path, output, maximum, deadline):
        self.calls.append(('BINARY', path, None, None, {}))
        if len(self.data) > maximum: raise GVError('limit', 'download_limit')
        output.write(self.data)
        return {'bytes': len(self.data), 'sha256': hashlib.sha256(self.data).hexdigest()}


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.client = Fake(); self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name); self.file = self.root / 'input.zip'; self.file.write_bytes(b'\x00\xffbinary\r\n')
        self.out = self.root / 'output.txt'
    def call(self, *args):
        return release_command(self.client, 'o/r', parser().parse_args(['release', *args, '-R', 'o/r']))
    def writes(self): return [c for c in self.client.calls if c[0] not in ('GET', 'BINARY')]

    def test_list_drafts_prereleases_and_post_scan_page_filters(self):
        self.client.rows[2] = release(2, 'v2', draft=False, prerelease=True)
        self.client.rows[3] = release(3, 'v3', draft=False)
        result = self.call('list', '--limit', '1', '--exclude-drafts', '--exclude-pre-releases')
        self.assertEqual([r['id'] for r in result['releases']], [3]); self.assertEqual(result['coverage']['entries_scanned'], 3)
        self.assertEqual(result['coverage']['scope'], 'visible_to_token')

    def test_empty_page_404_needs_reachable_repository(self):
        self.client.rows = {}
        self.assertEqual(self.call('list')['releases'], [])
        original = self.client.request; reads = 0
        def inaccessible(method, path, *args, **kwargs):
            nonlocal reads
            if path == 'repos/o/r':
                reads += 1
                if reads > 1: raise GVError('repo disappeared', 'http_error', 404)
            return original(method, path, *args, **kwargs)
        self.client.request = inaccessible
        with self.assertRaises(GVError): self.call('list')

    def test_catalog_duplicate_and_page_bound_errors(self):
        with self.assertRaises(GVError) as error: self.call('list', '--max-pages', '1')
        self.assertEqual(error.exception.code, 'pagination_limit')
        original = self.client.request
        def repeat(method, path, query=None, **kwargs):
            if path.endswith('/releases') and method == 'GET': return [release()]
            return original(method, path, query, **kwargs)
        self.client.request = repeat
        with self.assertRaises(GVError) as error: self.call('list')
        self.assertEqual(error.exception.code, 'pagination_error')

    def test_numeric_and_slash_tags_resolve_by_exact_catalog_name(self):
        for name in ('123', 'version/test'):
            self.client.rows[1]['tag_name'] = name
            self.assertEqual(self.call('view', name)['id'], 1)
        with self.assertRaises(GVError) as error: self.call('view', 'missing')
        self.assertEqual(error.exception.code, 'release_not_found')

    def test_create_pins_target_and_preserves_notes_bytes(self):
        notes = self.root / 'notes'; notes.write_bytes('Привет\r\n$HOME $(literal)'.encode())
        result = self.call('create', 'new', '--target', 'develop', '--draft', '--prerelease', '-F', str(notes))
        payload = self.writes()[0][3]
        self.assertEqual(payload['target_commitish'], SHA)
        self.assertEqual(payload['body'].encode(), notes.read_bytes())
        self.assertEqual(result['release']['id'], 2)

    def test_duplicate_create_and_missing_edit_flags_refused(self):
        with self.assertRaises(GVError): self.call('create', 'v1', '--target', SHA)
        with self.assertRaises(GVError): self.call('edit', 'v1')
        self.assertEqual(self.writes(), [])

    def test_full_target_sha_must_be_confirmed_before_post(self):
        with self.assertRaises(GVError) as error: self.call('create', 'new', '--target', 'b' * 40)
        self.assertEqual(error.exception.code, 'release_target_mismatch'); self.assertEqual(self.writes(), [])

    def test_edit_publish_notes_clear_and_noop(self):
        result = self.call('edit', 'v1', '--publish', '-n', '', '-t', 'Published')
        self.assertFalse(result['release']['draft']); self.assertEqual(result['release']['body'], '')
        self.assertFalse(self.call('edit', 'v1', '--publish')['changed'])
        self.assertEqual(len(self.writes()), 1)

    def test_clear_nonempty_notes_refused_before_combined_mutation(self):
        self.client.rows[1]['body'] = 'Existing notes'
        with self.assertRaises(GVError) as error: self.call('edit', 'v1', '--publish', '-n', '')
        self.assertEqual(error.exception.code, 'backend_unsupported')
        self.assertEqual(self.writes(), []); self.assertTrue(self.client.rows[1]['draft'])

    def test_ignored_edit_and_delete_are_not_success(self):
        self.client.ignore = True
        for args in (('edit', 'v1', '--publish'), ('delete', 'v1')):
            with self.assertRaises(GVError) as error: self.call(*args)
            self.assertEqual(error.exception.code, 'verification_failed')

    def test_release_delete_verifies_absence_and_does_not_cleanup_tag(self):
        result = self.call('delete', 'v1')
        self.assertTrue(result['deleted']); self.assertFalse(result['tag_cleanup_requested'])
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.writes()[0][1], 'repos/o/r/releases/1')

    def test_partial_create_keeps_id_and_no_retry_on_denial(self):
        self.client.fail_verify = True
        with self.assertRaises(GVError) as error: self.call('create', 'new', '--target', SHA)
        self.assertEqual(error.exception.details['release_id'], 2)
        self.assertTrue(error.exception.details['mutation_attempted']); self.assertEqual(len(self.writes()), 1)
        self.client = Fake(); self.client.deny = True
        with self.assertRaises(GVError) as error: self.call('edit', 'v1', '--publish')
        self.assertEqual(error.exception.status, 403); self.assertEqual(len(self.writes()), 1)

    def test_concurrent_release_preflight_refuses_mutation(self):
        original = self.client.request; reads = 0
        def changing(method, path, *args, **kwargs):
            nonlocal reads
            if path.endswith('/releases/1'):
                reads += 1
                if reads == 2: self.client.rows[1]['body'] = 'Concurrent'
            return original(method, path, *args, **kwargs)
        self.client.request = changing
        with self.assertRaises(GVError) as error: self.call('edit', 'v1', '--publish')
        self.assertEqual(error.exception.code, 'concurrent_change'); self.assertEqual(self.writes(), [])

    def test_upload_binary_multipart_and_duplicate_refusal(self):
        result = self.call('upload', 'v1', str(self.file))
        self.assertEqual(result['asset']['name'], 'input.zip')
        self.assertEqual(result['sha256'], hashlib.sha256(self.file.read_bytes()).hexdigest())
        self.assertEqual(self.client.data, self.file.read_bytes())
        request = self.writes()[0]
        self.assertEqual(request[2], {'name': 'input.zip'})
        self.assertIn(b'name="attachment"; filename="input.zip"', request[4]['encoded_body'])
        with self.assertRaises(GVError) as error: self.call('upload', 'v1', str(self.file))
        self.assertEqual(error.exception.code, 'asset_exists'); self.assertEqual(len(self.writes()), 1)

    def test_upload_nonregular_size_and_bad_names_refused(self):
        link = self.root / 'link.zip'; link.symlink_to(self.file)
        fifo = self.root / 'pipe.zip'; os.mkfifo(fifo)
        for args in ((str(link),), (str(fifo),), (str(self.file), '--max-bytes', '1'), (str(self.file), '--name', ''), (str(self.file), '--name', 'bad/name.zip'), (str(self.file), '--name', 'bad\r\n.txt')):
            with self.assertRaises(GVError): self.call('upload', 'v1', *args)
        self.assertEqual(self.writes(), [])

    def test_download_exact_bytes_public_uuid_only_and_sha256(self):
        result = self.call('download', 'v1', '--name', 'test.txt', '-o', str(self.out))
        self.assertEqual(self.out.read_bytes(), self.client.data)
        self.assertEqual(result['sha256'], hashlib.sha256(self.client.data).hexdigest())
        self.assertEqual([c[1] for c in self.client.calls if c[0] == 'BINARY'], ['attachments/' + UUID])

    def test_existing_and_racing_destination_never_overwritten(self):
        self.out.write_bytes(b'keep')
        with self.assertRaises(GVError): self.call('download', 'v1', '--name', 'test.txt', '-o', str(self.out))
        self.assertEqual(self.client.calls, []); self.out.unlink()
        original = self.client.download_to
        def race(*args):
            self.out.write_bytes(b'concurrent user data')
            return original(*args)
        self.client.download_to = race
        with self.assertRaises(GVError): self.call('download', 'v1', '--name', 'test.txt', '-o', str(self.out))
        self.assertEqual(self.out.read_bytes(), b'concurrent user data'); self.assertFalse(list(self.root.glob('.gv-release-*')))

    def test_changed_or_short_download_is_not_published(self):
        self.client.files[0]['size'] += 1
        with self.assertRaises(GVError) as error: self.call('download', 'v1', '--name', 'test.txt', '-o', str(self.out))
        self.assertEqual(error.exception.code, 'incomplete_download'); self.assertFalse(self.out.exists())
        self.client = Fake(); original = self.client.download_to
        def change(*args):
            result = original(*args); self.client.files[0]['uuid'] = '1ad10e5d-1b93-4c64-9e80-54264081bba4'; return result
        self.client.download_to = change
        with self.assertRaises(GVError) as error: self.call('download', 'v1', '--name', 'test.txt', '-o', str(self.out))
        self.assertEqual(error.exception.code, 'concurrent_change'); self.assertFalse(self.out.exists())

    def test_download_limit_and_unknown_asset(self):
        for flags in (['--name', 'test.txt', '--max-bytes', '1'], ['--name', 'missing']):
            with self.assertRaises(GVError): self.call('download', 'v1', *flags, '-o', str(self.out))
        self.assertFalse(self.out.exists()); self.assertFalse(any(c[0] == 'BINARY' for c in self.client.calls))

    def test_delete_asset_confirms_remaining_catalog(self):
        self.assertTrue(self.call('delete-asset', 'v1', 'test.txt')['deleted'])
        self.assertEqual(self.client.files, [])
        self.client = Fake(); self.client.ignore = True
        with self.assertRaises(GVError) as error: self.call('delete-asset', 'v1', 'test.txt')
        self.assertEqual(error.exception.code, 'verification_failed')

    def test_bad_uuid_or_duplicate_asset_name_refused(self):
        self.client.files[0]['uuid'] = 'not-a-uuid'
        with self.assertRaises(GVError): self.call('assets', 'v1')
        self.client = Fake(); self.client.files.append(asset(identity=6))
        with self.assertRaises(GVError): self.call('assets', 'v1')

    def test_dry_run_skips_file_reads_and_backend_refusal(self):
        self.client.dry_run = True
        result = self.call('upload', 'v1', '/not-existing.zip')
        self.assertIn('plan', result); self.assertEqual(self.client.calls, [])
        self.assertIn('plan', self.call('download', 'v1', '--name', 'test.txt', '-o', str(self.out)))
        self.assertFalse(self.out.exists())
        with self.assertRaises(GVError): execute(parser().parse_args(['release', 'list', '-R', 'o/r', '--backend', 'web', '--dry-run']))


class Response(io.BytesIO):
    status = 200
    headers = {}


class ReleaseTransportTests(unittest.TestCase):
    def test_encoded_multipart_body_and_no_sensitive_dry_run(self):
        client = Client(dry_run=True)
        self.assertEqual(client.request('POST', 'p', encoded_body=b'private bytes', content_type='multipart/form-data; boundary=abc')['body'], {'encoded_bytes': 13})
        client.dry_run = False; client.opener = Mock(); client.opener.open.return_value = Response(b'{}')
        client.request('POST', 'p', encoded_body=b'\x00\xff', content_type='multipart/form-data; boundary=abc')
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.data, b'\x00\xff'); self.assertIn('boundary=abc', request.get_header('Content-type'))
        self.assertEqual(client.opener.open.call_count, 1)

    def test_encoded_upload_rejects_backend_method_and_header_injection(self):
        client = Client(dry_run=True)
        for method, content_type in (('GET', 'multipart/form-data'), ('POST', 'bad\r\nheader'), ('POST', None)):
            with self.assertRaises(GVError): client.request(method, 'p', encoded_body=b'a', content_type=content_type)
        with self.assertRaises(GVError): client.request('POST', 'p', body={}, encoded_body=b'a', content_type='x')
        with self.assertRaises(GVError): Client('web', dry_run=True).request('POST', 'p', encoded_body=b'a', content_type='x')

    def test_partial_binary_http_206_refused(self):
        client = Client(dry_run=True); client.dry_run = False; client.opener = Mock()
        response = Response(b'partial'); response.status = 206; client.opener.open.return_value = response
        output = io.BytesIO()
        with self.assertRaises(GVError): client.download_to('attachments/' + UUID, output, 100, time.monotonic() + 10)
        self.assertEqual(output.getvalue(), b'')

    def test_multipart_mutation_not_retried_on_http_429(self):
        client = Client(dry_run=True); client.dry_run = False; client.opener = Mock()
        client.opener.open.side_effect = urllib.error.HTTPError('https://api.gitverse.ru/p', 429, 'limited', {'Retry-After': '1'}, None)
        with self.assertRaises(GVError): client.request('POST', 'p', encoded_body=b'file', content_type='multipart/form-data; boundary=abc')
        self.assertEqual(client.opener.open.call_count, 1)
