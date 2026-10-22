import copy
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.search import search_code, search_repos
from test_contents import FakeClient, SHA, blob, file_entry


class Catalog:
    dry_run = False
    def __init__(self):
        self.calls = []
        self.pages = {1: [{'id': 1, 'name': 'tool', 'full_name': 'owner/tool', 'description': 'Привет $HOME', 'private': True}],
                      2: [{'id': 2, 'name': 'second', 'full_name': 'owner/second', 'description': None, 'private': False}], 3: []}
    def request(self, method, path, query=None, **kwargs):
        self.calls.append((method, path, query, kwargs))
        return copy.deepcopy(self.pages[query['page']])


class SearchTests(unittest.TestCase):
    def code(self, data=b'hello\r\nHELLO\n$HOME $(literal)', query='hello', *flags, client=None):
        self.client = client or FakeClient(data if data is not None else b'hello\r\nHELLO\n$HOME $(literal)')
        return search_code(self.client, 'o/r', parser().parse_args(['search', 'code', query, '-R', 'o/r', *flags]))

    def repos(self, query='tool', *flags, client=None):
        self.client = client or Catalog()
        return search_repos(self.client, parser().parse_args(['search', 'repos', query, *flags]))

    def test_code_pinned_hash_line_numbers_and_crlf(self):
        result = self.code()
        self.assertEqual(result['commit_sha'], SHA)
        self.assertEqual([r['line_number'] for r in result['results']], [1, 2])
        self.assertEqual([r['text'] for r in result['results']], ['hello', 'HELLO'])
        content = [r for r in self.client.calls if '/contents/' in r[1]]
        self.assertEqual(len(content), 1); self.assertEqual(content[0][2]['ref'], SHA)
        self.assertEqual(len([r for r in self.client.calls if '/git/trees/' in r[1]]), 1)

    def test_literal_and_case_sensitive_and_unicode_casefold(self):
        self.assertEqual(self.code(query='$HOME $(literal)')['total_matches'], 1)
        self.assertEqual(self.code(query='.*')['total_matches'], 0)
        self.assertEqual(self.code(b'hello\nHELLO', 'hello', '--case-sensitive')['total_matches'], 1)
        self.assertEqual(self.code('Straße\nПривет'.encode(), 'STRASSE')['total_matches'], 1)

    def test_binary_empty_and_special_coverage(self):
        result = self.code(b'hello\0world')
        self.assertEqual(result['total_matches'], 0)
        self.assertEqual(result['coverage']['binary_files_skipped'], ['folder/Привет.txt'])
        self.assertEqual(self.code(b'')['total_matches'], 0)
        client = FakeClient()
        client.rows.append({'path': 'link', 'type': 'blob', 'mode': '120000', 'sha': blob(b'hello'), 'size': 5})
        result = self.code(client=client)
        self.assertEqual(result['coverage']['special_entries_skipped'], [{'path': 'link', 'type': 'symlink'}])

    def test_scope_file_directory_and_missing_path(self):
        self.assertEqual(self.code(None, 'hello', '--path', 'folder')['total_matches'], 2)
        self.assertEqual(self.code(None, 'hello', '--path', 'folder/Привет.txt')['coverage']['files_read'], 1)
        with self.assertRaises(GVError) as error: self.code(None, 'hello', '--path', 'absent')
        self.assertEqual(error.exception.code, 'path_not_found')

    def test_byte_and_file_bounds_before_blob_fetch(self):
        for flags in (['--max-bytes', '1'], ['--max-total-bytes', '1']):
            client = FakeClient()
            with self.assertRaises(GVError): self.code(None, 'hello', *flags, client=client)
            self.assertFalse(any('/contents/' in r[1] for r in client.calls))
        client = FakeClient()
        client.rows.append(file_entry(b'other', 'other'))
        with self.assertRaises(GVError): self.code(None, 'hello', '--max-files', '1', client=client)
        self.assertFalse(any('/contents/' in r[1] for r in client.calls))

    def test_match_bound_errors_without_partial_result(self):
        with self.assertRaises(GVError) as error: self.code(None, 'hello', '--max-matches', '1')
        self.assertEqual(error.exception.code, 'search_limit')

    def test_corrupt_blob_not_searchable(self):
        client = FakeClient(); client.content_changes = {'content': 'aGVsbG8='}
        with self.assertRaises(GVError): self.code(client=client)

    def test_unicode_separator_is_not_a_line_boundary(self):
        result = self.code('first\u2028hello\rhello'.encode())
        self.assertEqual([r['line_number'] for r in result['results']], [1, 2])
        self.assertIn('\u2028', result['results'][0]['text'])

    def test_repos_complete_scan_even_when_first_page_short(self):
        result = self.repos('SECOND')
        self.assertEqual(result['results'][0]['id'], 2)
        self.assertEqual(result['coverage']['repositories_scanned'], 2)
        self.assertEqual(len(self.client.calls), 3)
        self.assertEqual(result['scope'], 'authenticated_user_owned')

    def test_repository_names_descriptions_owner_and_case(self):
        self.assertEqual(self.repos('$HOME')['total_matches'], 1)
        self.assertEqual(self.repos('OWNER/TOOL')['total_matches'], 1)
        self.assertEqual(self.repos('TOOL', '--case-sensitive')['total_matches'], 0)
        result = self.repos('tool', '--owner', 'other')
        self.assertEqual(result['scope'], 'user_public')
        self.assertEqual(self.client.calls[0][1], 'users/other/repos')

    def test_repository_repeated_pages_and_changed_metadata_fail(self):
        client = Catalog(); client.pages[2] = client.pages[1]
        with self.assertRaises(GVError) as error: self.repos(client=client)
        self.assertEqual(error.exception.code, 'pagination_error')
        client = Catalog(); del client.pages[1][0]['private']
        with self.assertRaises(GVError): self.repos(client=client)

    def test_repository_page_entry_bounds_and_unknown_envelope(self):
        for flags in (['--max-pages', '1'], ['--max-entries', '1']):
            with self.assertRaises(GVError): self.repos('second', *flags)
        client = Catalog(); client.pages[1] = {'items': []}
        with self.assertRaises(GVError): self.repos(client=client)

    def test_dry_runs_no_network(self):
        for action, function in (('code', search_code), ('repos', search_repos)):
            args = parser().parse_args(['search', action, 'literal', '--dry-run'])
            client = FakeClient(); client.dry_run = True
            result = function(client, 'o/r', args) if action == 'code' else function(client, args)
            self.assertIn('plan', result); self.assertEqual(client.calls, [])

    def test_invalid_queries_owner_backend_and_repository_flag(self):
        for value in ('', ' ', 'two\nlines', '\0'):
            with self.assertRaises(GVError): self.code(query=value)
        for flags in (['--owner', '../other'], ['--page-size', '101']):
            with self.assertRaises(GVError): self.repos('hello', *flags)
        with self.assertRaises(GVError): execute(parser().parse_args(['search', 'repos', 'test', '-R', 'o/r', '--dry-run']))
        with self.assertRaises(GVError): execute(parser().parse_args(['search', 'code', 'test', '-R', 'o/r', '--backend', 'web', '--dry-run']))

    def test_deadline_error_not_converted_to_empty_results(self):
        client = FakeClient()
        with patch.object(client, 'request', side_effect=GVError('deadline', 'deadline_exceeded')):
            with self.assertRaises(GVError) as error: self.code(client=client)
        self.assertEqual(error.exception.code, 'deadline_exceeded')
