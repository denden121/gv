import hashlib
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from gitverse_cli.cli import parser, execute
from gitverse_cli.client import Client, GVError
from gitverse_cli.pr_create_attachments import plan


class AttachPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root / 'login.error.png'
        self.image.write_bytes(b'synthetic image bytes')
        self.video = self.root / 'clip.mp4'
        self.video.write_bytes(b'synthetic video bytes')

    def run_plan(self, values):
        return plan(values, time.monotonic() + 5)

    def create(self, *flags):
        args = parser().parse_args(['pr', 'create', '-R', 'synthetic/repo', '--head', 'feature',
                                    '--base', 'main', '--title', 'Synthetic', *flags])
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')):
            return execute(args)

    def test_filename_alt_video_and_hash_no_payload(self):
        result = self.run_plan([str(self.image), str(self.video)])
        a, v = result['files']
        self.assertEqual(a['alt'], 'login.error.png')
        self.assertIsNone(v['alt'])
        self.assertEqual(a['sha256'], hashlib.sha256(self.image.read_bytes()).hexdigest())
        self.assertEqual(a['size'], self.image.stat().st_size)
        self.assertNotIn('data', a)
        self.assertFalse(result['final_body_known'])
        self.assertFalse(result['upload_contract_verified'])

    def test_explicit_alt_and_literal_hash_filename(self):
        self.assertEqual(self.run_plan([str(self.image) + '#Ошибка [login]'])['files'][0]['alt'], 'Ошибка [login]')
        self.assertEqual(self.run_plan([str(self.image) + '#'])['files'][0]['alt'], '')
        hashed = self.root / 'literal#name.png'
        hashed.write_bytes(b'image')
        self.assertEqual(self.run_plan([str(hashed)])['files'][0]['name'], hashed.name)
        self.assertEqual(self.run_plan([str(hashed) + '#alt#tail'])['files'][0]['alt'], 'alt#tail')

    def test_body_file_stdin_and_metadata_remain_original_unresolved(self):
        body = '![body alt](./login.error.png)\r\n'
        file = self.root / 'body.md'; file.write_bytes(body.encode())
        for flags in [('--body', body), ('--body-file', str(file)), ('--body-file', '-')]:
            with self.subTest(flags=flags[0]), patch('sys.stdin', io.StringIO(body)):
                result = self.create('--attach', str(self.image), '--assignee', '@me', '--label', 'bug',
                                     '--reviewer', 'alice', '--dry-run', *flags)
                self.assertTrue(result['creation_plan_body_is_original'])
                self.assertFalse(result['attachments']['final_body_known'])
                self.assertNotIn('number', result)
                self.assertEqual(result['creation_plan']['base_request_template']['body']['body'], body)

    def test_plain_dryrun_no_credentials_input_or_network(self):
        with patch.dict(os.environ, {'GITVERSE_TOKEN': 'synthetic-only', 'GITVERSE_COOKIE_FILE': '/missing'}), \
             patch('builtins.input', side_effect=AssertionError('prompt')):
            result = self.create('--attach', str(self.image), '--dry-run')
        self.assertFalse(result['mutation_attempted'])
        self.assertEqual(result['creation_plan']['body']['title'], 'Synthetic')
        self.assertNotIn('synthetic-only', str(result))

    def test_real_mode_refuses_before_stdin_or_any_http_without_retry(self):
        with patch('gitverse_cli.actions_config.read_value', side_effect=AssertionError('stdin')), \
             patch.object(Client, 'request', side_effect=AssertionError('HTTP')):
            for _ in range(2):
                with self.assertRaises(GVError) as caught:
                    self.create('--attach', str(self.image), '--body-file', '-')
                self.assertEqual(caught.exception.code, 'feature_unavailable')
                self.assertFalse(caught.exception.details['mutation_attempted'])
                self.assertEqual(caught.exception.details['mutation_outcome'], 'none')

    def test_invalid_values_and_video_alt_even_empty(self):
        for values in [[], [str(self.image)] * 51, [''], ['\0'], ['x\udcff.png'], ['x'*16385],
                       [str(self.video)+'#alt'], [str(self.video)+'#'], ['-'], ['x.txt'], ['no.png']]:
            with self.subTest(value=repr(values)[:70]), self.assertRaises(GVError):
                self.run_plan(values)

    def test_symlink_hardlink_fifo_empty_directory_refused(self):
        sym = self.root / 'sym.png'; sym.symlink_to(self.image)
        hard = self.root / 'hard.png'; os.link(self.image, hard)
        fifo = self.root / 'fifo.png'; os.mkfifo(fifo)
        empty = self.root / 'empty.png'; empty.touch()
        directory = self.root / 'dir.png'; directory.mkdir()
        for values in [[str(sym)], [str(self.image), str(hard)], [str(fifo)], [str(empty)], [str(directory)]]:
            with self.subTest(values=values), self.assertRaises(GVError): self.run_plan(values)

    def test_size_total_deadline_and_changed_file_refused(self):
        big = self.root / 'big.png'
        with big.open('wb') as f: f.truncate(10*1024*1024+1)
        with self.assertRaises(GVError) as e: self.run_plan([str(big)])
        self.assertEqual(e.exception.code, 'upload_limit')
        with self.video.open('wb') as f: f.truncate(100*1024*1024+1)
        with self.assertRaises(GVError) as e: self.run_plan([str(self.video)])
        self.assertEqual(e.exception.code, 'upload_limit')
        with patch('gitverse_cli.pr_create_attachments.TOTAL', len(self.image.read_bytes())+1):
            other = self.root/'other.png'; other.write_bytes(b'xx')
            with self.assertRaises(GVError) as e: self.run_plan([str(self.image), str(other)])
            self.assertEqual(e.exception.code, 'upload_limit')
        with self.assertRaises(GVError) as e: plan([str(self.image)], time.monotonic()-1)
        self.assertEqual(e.exception.code, 'deadline_exceeded')
        with patch('gitverse_cli.pr_create_attachments.read_upload', side_effect=GVError('changed', 'concurrent_change')):
            with self.assertRaises(GVError) as e: self.run_plan([str(self.image)])
            self.assertEqual(e.exception.code, 'concurrent_change')

    def test_fifty_files_boundary_and_duplicates(self):
        files = []
        for i in range(50):
            f = self.root / (str(i)+'.png'); f.write_bytes(b'x'); files.append(str(f))
        self.assertEqual(len(self.run_plan(files)['files']), 50)
        with self.assertRaises(GVError): self.run_plan([str(self.image)]*2)

    def test_offline_base_required_and_body_errors(self):
        for flags in [('--base', ''), ('--body', '\0'), ('--body', 'x'*1048577)]:
            with self.assertRaises(GVError): self.create('--attach', str(self.image), '--dry-run', *flags)
