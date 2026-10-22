import copy
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.repo_settings import FIELDS, edit_repository


def repository():
    return {'id': 7, 'full_name': 'me/demo', 'private': True, 'description': 'old', 'homepage': None,
            'default_branch': 'master', 'allow_merge_commit': True, 'allow_squash_merge': True,
            'allow_rebase_merge': True, 'delete_branch_on_merge': False, 'is_template': False}


class Fake:
    backend = 'public'
    dry_run = False

    def __init__(self):
        self.row = repository(); self.calls = []; self.reads = 0
        self.branch_pages = [[{'name': 'master'}, {'name': 'feature/demo'}], []]
        self.ignore = False; self.failure = None; self.hook = None

    def request(self, method, path, query=None, body=None, **kwargs):
        self.calls.append((method, path, query, copy.deepcopy(body), kwargs))
        if method == 'GET':
            if path.endswith('/branches'): return copy.deepcopy(self.branch_pages[query['page']-1])
            self.reads += 1
            if self.hook: self.hook(self)
            return copy.deepcopy(self.row)
        if self.failure: raise self.failure
        if not self.ignore:
            reverse = {v: k for k, v in FIELDS.items()}
            self.row.update({reverse[k]: v for k, v in body.items()})
        return copy.deepcopy(self.row)


class RepoSettingsTests(unittest.TestCase):
    def setUp(self): self.client = Fake()

    def call(self, *args):
        return edit_repository(self.client, 'me/demo', parser().parse_args(['repo', 'edit', *args]))

    def writes(self): return [c for c in self.client.calls if c[0] != 'GET']

    def test_description_homepage_patch_readback(self):
        result = self.call('--description', 'Привет\n$HOME $(literal)', '--homepage', 'https://example.org/path?q=x')
        self.assertTrue(result['changed']); self.assertTrue(result['verification']['readback'])
        self.assertEqual(self.writes()[0][3], {'description': 'Привет\n$HOME $(literal)', 'homepage': 'https://example.org/path?q=x'})
        self.assertFalse(result['verification']['atomic_compare_and_swap'])
        self.assertEqual(self.client.reads, 3)

    def test_equal_values_noop_and_nullable_text(self):
        result = self.call('--description', 'old', '--homepage', '', '--default-branch', 'master')
        self.assertFalse(result['changed']); self.assertEqual(self.writes(), [])
        self.assertEqual(len(self.client.calls), 1)

    def test_empty_text_is_explicit_not_omitted(self):
        self.call('--description', '')
        self.assertEqual(self.writes()[0][3], {'description': ''})

    def test_all_boolean_field_mappings_true_and_false(self):
        options = {'merge-commit': 'allow_merge_commit', 'squash-merge': 'allow_squash_merge',
                   'rebase-merge': 'allow_rebase_merge',
                   'template': 'is_template'}
        body_fields = {'allow_merge_commit': 'allow_merge', 'allow_squash_merge': 'allow_squash_merge',
                       'allow_rebase_merge': 'allow_rebase',
                       'is_template': 'is_template'}
        for option, key in options.items():
            for value in (False, True):
                self.client = Fake(); self.client.row[key] = not value
                self.call(('--enable-' if value else '--disable-') + option)
                self.assertEqual(self.writes()[0][3], {body_fields[key]: value})

    def test_patch_only_changed_fields(self):
        self.call('--description', 'old', '--disable-squash-merge')
        self.assertEqual(self.writes()[0][3], {'allow_squash_merge': False})

    def test_default_branch_exact_existing_slash_name(self):
        self.call('--default-branch', 'feature/demo')
        self.assertEqual(self.writes()[0][3], {'default_branch': 'feature/demo'})
        branches = [c for c in self.client.calls if c[1].endswith('/branches')]
        self.assertEqual([c[2]['page'] for c in branches], [1, 2])

    def test_tag_sha_or_missing_branch_refused(self):
        for name in ('not-a-branch', 'a' * 40):
            with self.assertRaises(GVError) as cm: self.call('--default-branch', name)
            self.assertEqual(cm.exception.code, 'branch_not_found')
        self.assertEqual(self.writes(), [])

    def test_branch_page_bound_even_after_match(self):
        with self.assertRaises(GVError) as cm: self.call('--default-branch', 'feature/demo', '--max-pages', '1')
        self.assertEqual(cm.exception.code, 'pagination_limit'); self.assertEqual(self.writes(), [])

    def test_bad_branch_catalog_refuses_write(self):
        for batches in ([[{'name': 'feature/demo'}], [{'name': 'feature/demo'}]], [[{'id': 1}], []], [{'branches': []}]):
            self.client = Fake(); self.client.branch_pages = batches
            with self.assertRaises(GVError): self.call('--default-branch', 'feature/demo')
            self.assertEqual(self.writes(), [])

    def test_unknown_settings_or_identity_refuses_write(self):
        for key, value in (('id', True), ('full_name', 'other/demo'), ('allow_merge_commit', 'true'), ('description', []), ('private', None)):
            self.client = Fake(); self.client.row[key] = value
            with self.assertRaises(GVError): self.call('--description', 'new', '--disable-merge-commit')
            self.assertEqual(self.writes(), [])
        self.client = Fake(); del self.client.row['description']
        with self.assertRaises(GVError): self.call('--description', 'new')

    def test_concurrent_requested_change_or_replacement_refused(self):
        for key, value in (('description', 'raced'), ('id', 8), ('private', False)):
            self.client = Fake()
            def change(c):
                if c.reads == 2: c.row[key] = value
            self.client.hook = change
            with self.assertRaises(GVError) as cm: self.call('--description', 'new')
            self.assertEqual(cm.exception.code, 'concurrent_change')
            self.assertFalse(cm.exception.details['mutation_attempted']); self.assertEqual(self.writes(), [])

    def test_unrelated_concurrent_field_preserved(self):
        def change(c):
            if c.reads == 2: c.row['is_template'] = True
        self.client.hook = change
        self.call('--description', 'new')
        self.assertTrue(self.client.row['is_template'])

    def test_ignored_patch_is_failure_with_recovery(self):
        self.client.ignore = True
        with self.assertRaises(GVError) as cm: self.call('--description', 'new')
        self.assertEqual(cm.exception.code, 'verification_failed')
        self.assertEqual(cm.exception.details['stage'], 'verify')
        self.assertTrue(cm.exception.details['mutation_attempted'])
        self.assertEqual(len(self.writes()), 1)

    def test_unrequested_setting_change_after_patch_is_not_silent_success(self):
        def change(c):
            if c.reads == 3: c.row['allow_merge_commit'] = False
        self.client.hook = change
        with self.assertRaises(GVError) as cm: self.call('--description', 'new')
        self.assertEqual(cm.exception.code, 'verification_failed')
        self.assertEqual(cm.exception.details['stage'], 'verify')

    def test_failed_patch_is_not_retried(self):
        self.client.failure = GVError('no access', 'http_error', 403)
        with self.assertRaises(GVError) as cm: self.call('--description', 'new')
        self.assertEqual(cm.exception.status, 403); self.assertEqual(cm.exception.details['stage'], 'write')
        self.assertEqual(len(self.writes()), 1)

    def test_verification_network_failure_keeps_partial_context(self):
        def change(c):
            if c.reads == 3: raise GVError('timeout', 'network_error')
        self.client.hook = change
        with self.assertRaises(GVError) as cm: self.call('--description', 'new')
        self.assertEqual(cm.exception.details['repository_id'], 7)
        self.assertTrue(cm.exception.details['mutation_attempted'])
        self.assertEqual(self.client.row['description'], 'new')

    def test_empty_request_invalid_url_branch_and_oversize_before_network(self):
        for args in ((), ('--homepage', 'javascript:alert(1)'), ('--homepage', 'https://u:p@example.org'),
                     ('--homepage', 'https://example.org/a b'), ('--homepage', 'https://[bad'),
                     ('--homepage', 'https://example.org:invalid'), ('--description', '\0'),
                     ('--description', 'я' * 8193), ('--default-branch', '../x')):
            with self.assertRaises(GVError): self.call(*args)
        self.assertEqual(self.client.calls, [])

    def test_dry_run_payload_without_network(self):
        self.client.dry_run = True
        result = self.call('--disable-merge-commit', '--description', '')
        self.assertEqual(result['body'], {'allow_merge': False, 'description': ''})
        self.assertEqual(self.client.calls, [])

    def test_cli_dispatch_and_backend_guard(self):
        with patch('gitverse_cli.cli.Client', return_value=self.client):
            result = execute(parser().parse_args(['repo', 'edit', '-R', 'me/demo', '--description', 'new']))
            self.assertTrue(result['changed'])
            self.client.backend = 'web'
            with self.assertRaises(GVError): execute(parser().parse_args(['repo', 'edit', '-R', 'me/demo', '--description', 'new']))
        self.assertTrue(self.client.calls[0][4]['require_complete'])
        self.assertGreater(self.client.calls[0][4]['deadline'], 0)
