import copy
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gitverse_cli.cli import body_text, execute, parser
from gitverse_cli.client import GVError
from gitverse_cli.issues import issue_command, issue_target, label_command


def label(name='bug', identity=1, **fields):
    return {'id': identity, 'name': name, 'color': '33aaff', 'description': '', 'exclusive': False, 'is_archived': False, **fields}


def issue(number=1, **fields):
    return {'id': 100 + number, 'number': number, 'title': 'Original', 'body': 'Original body', 'state': 'open',
            'pull_request': None, 'labels': [], 'assignees': [], 'user': {'login': 'me'}, **fields}


class Fake:
    dry_run = False
    backend = 'public'
    def __init__(self):
        self.calls = []; self.labels = [label(), label('keep', 2)]; self.issues = {1: issue()}; self.comments = {}
        self.ignore = False; self.fail_verify = False; self.deny = False; self.mutated = False
    def request(self, method, path, query=None, body=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(query), copy.deepcopy(body)))
        if self.fail_verify and self.mutated and method == 'GET': raise GVError('verify read failed', 'network_error')
        if method != 'GET':
            if self.deny: raise GVError('Forbidden', 'http_error', 403)
            self.mutated = True
        if path == 'user': return {'login': 'me', 'id': 7}
        if path.startswith('users/'): return {'login': path.split('/')[1], 'id': 7}
        if path.endswith('/labels'):
            if method == 'GET': return copy.deepcopy(self.labels if query['page'] == 1 else [])
            row = label(body['name'], 3, **{k: v for k, v in body.items() if k != 'name'})
            self.labels.append(row); return copy.deepcopy(row)
        if '/labels/' in path:
            row = next(r for r in self.labels if r['id'] == int(path.split('/')[-1]))
            if method == 'PATCH' and not self.ignore: row.update(body)
            return copy.deepcopy(row)
        if '/issues/comments/' in path: return copy.deepcopy(self.comments[int(path.split('/')[-1])])
        if path.endswith('/comments'):
            if method == 'GET': return copy.deepcopy(list(self.comments.values()))
            row = {'id': 80, 'body': body['body']}; self.comments[80] = row; return copy.deepcopy(row)
        if path.endswith('/issues'):
            if method == 'GET': return copy.deepcopy(list(self.issues.values()) if query['page'] == 1 else [])
            row = issue(2, **{k: v for k, v in body.items() if k not in ('labels', 'assignees')})
            row['labels'] = [copy.deepcopy(r) for r in self.labels if r['name'] in body.get('labels', [])]
            row['assignees'] = [{'login': login} for login in body.get('assignees', [])]
            self.issues[2] = row; return copy.deepcopy(row)
        if '/issues/' in path:
            row = self.issues[int(path.split('/')[-1])]
            if method == 'PATCH' and not self.ignore:
                for k, v in body.items():
                    if k == 'labels': row[k] = [copy.deepcopy(r) for r in self.labels if r['name'] in v]
                    elif k == 'assignees': row[k] = [{'login': login} for login in v]
                    else: row[k] = v
            return copy.deepcopy(row)
        raise AssertionError((method, path))


class IssueTests(unittest.TestCase):
    def setUp(self): self.client = Fake()
    def run_command(self, group, *args):
        parsed = parser().parse_args([group, *args, '-R', 'o/r'])
        if group == 'issue': issue_target(parsed)
        return (issue_command if group == 'issue' else label_command)(self.client, 'o/r', parsed)
    def writes(self): return [c for c in self.client.calls if c[0] != 'GET']

    def test_create_label_names_not_ids_and_user_resolution(self):
        result = self.run_command('issue', 'create', '-t', 'New $HOME', '-b', 'Привет\r\nno LF', '-l', 'bug', '-a', '@me')
        self.assertEqual(result['issue']['number'], 2)
        self.assertEqual(self.writes()[0][3]['labels'], ['bug'])
        self.assertEqual(self.writes()[0][3]['assignees'], ['me'])
        self.assertEqual(result['issue']['body'], 'Привет\r\nno LF')

    def test_body_file_crlf_and_empty_body_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'body'; path.write_bytes('Привет\r\n$HOME '.encode())
            result = self.run_command('issue', 'create', '-t', 'New', '-F', str(path))
            self.assertEqual(result['issue']['body'].encode(), path.read_bytes())
        self.run_command('issue', 'edit', '1', '-b', '')
        self.assertEqual(self.client.issues[1]['body'], '')

    def test_edit_preserves_other_assignments_and_repeat_is_noop(self):
        self.client.issues[1].update(labels=[label('keep', 2)], assignees=[{'login': 'other'}])
        result = self.run_command('issue', 'edit', '1', '--add-label', 'bug', '--add-assignee', '@me')
        self.assertEqual({r['name'] for r in result['issue']['labels']}, {'bug', 'keep'})
        self.assertEqual({r['login'] for r in result['issue']['assignees']}, {'me', 'other'})
        self.assertFalse(self.run_command('issue', 'edit', '1', '--add-label', 'bug')['changed'])
        self.assertEqual(len(self.writes()), 1)

    def test_remove_last_assignments_sends_empty_arrays(self):
        self.client.issues[1].update(labels=[label()], assignees=[{'login': 'me'}])
        self.run_command('issue', 'edit', '1', '--remove-label', 'bug', '--remove-assignee', '@me')
        self.assertEqual(self.writes()[0][3], {'labels': [], 'assignees': []})

    def test_close_reopen_and_idempotence(self):
        self.assertEqual(self.run_command('issue', 'close', '1')['issue']['state'], 'closed')
        self.assertFalse(self.run_command('issue', 'close', '1')['changed'])
        self.assertEqual(self.run_command('issue', 'reopen', '1')['issue']['state'], 'open')
        self.assertEqual(len(self.writes()), 2)

    def test_pr_guard_all_single_issue_operations(self):
        self.client.issues[1]['pull_request'] = {}
        for args in (('view', '1'), ('close', '1'), ('reopen', '1'), ('edit', '1', '-t', 'x'), ('comment', '1', '-b', 'x'), ('comments', '1')):
            with self.assertRaises(GVError) as error: self.run_command('issue', *args)
            self.assertEqual(error.exception.code, 'issue_is_pull_request')
        self.assertEqual(self.writes(), [])

    def test_issue_list_excludes_pr_and_filters_before_paging(self):
        self.client.issues = {1: issue(1, pull_request={}), 2: issue(2, state='closed'), 3: issue(3, labels=[label()], assignees=[{'login': 'me'}], body='Привет')}
        result = self.run_command('issue', 'list', '-A', '@me', '-a', '@me', '-l', 'bug', '--search-text', 'ПРИВЕТ', '--limit', '1', '--paginate')
        self.assertEqual([r['number'] for r in result['issues']], [3])
        self.assertEqual(result['coverage']['entries_scanned'], 3)
        self.assertEqual(self.run_command('issue', 'list', '--state', 'closed')['issues'][0]['number'], 2)

    def test_duplicate_catalog_and_paging_bound_fail(self):
        original = self.client.request
        def repeating(method, path, query=None, **kwargs):
            if path.endswith('/issues'): return [issue()]
            return original(method, path, query, **kwargs)
        self.client.request = repeating
        with self.assertRaises(GVError) as error: self.run_command('issue', 'list')
        self.assertEqual(error.exception.code, 'pagination_error')
        with self.assertRaises(GVError) as error: self.run_command('issue', 'list', '--max-pages', '1')
        self.assertEqual(error.exception.code, 'pagination_limit')

    def test_unknown_archived_conflicting_and_exclusive_labels_preflight(self):
        for args in (('create', '-t', 'New', '-l', 'missing'), ('edit', '1', '--add-label', 'bug', '--remove-label', 'bug')):
            with self.assertRaises(GVError): self.run_command('issue', *args)
        self.client.labels[0]['is_archived'] = True
        with self.assertRaises(GVError): self.run_command('issue', 'create', '-t', 'New', '-l', 'bug')
        self.client.labels = [label('scope/a', exclusive=True), label('scope/b', 2, exclusive=True)]
        with self.assertRaises(GVError): self.run_command('issue', 'create', '-t', 'New', '-l', 'scope/a,scope/b')
        self.assertEqual(self.writes(), [])

    def test_concurrent_edit_rejected_before_patch(self):
        original = self.client.request; reads = 0
        def changing(method, path, *args, **kwargs):
            nonlocal reads
            if path.endswith('/issues/1'):
                reads += 1
                if reads == 2: self.client.issues[1]['title'] = 'Concurrent edit'
            return original(method, path, *args, **kwargs)
        self.client.request = changing
        with self.assertRaises(GVError) as error: self.run_command('issue', 'edit', '1', '-t', 'new')
        self.assertEqual(error.exception.code, 'concurrent_change'); self.assertEqual(self.writes(), [])

    def test_write_denial_and_unconfirmed_write_preserve_recovery(self):
        self.client.deny = True
        with self.assertRaises(GVError) as error: self.run_command('issue', 'create', '-t', 'new')
        self.assertEqual(error.exception.status, 403)
        self.assertTrue(error.exception.details['mutation_attempted']); self.assertEqual(len(self.writes()), 1)
        self.client = Fake(); self.client.fail_verify = True
        with self.assertRaises(GVError) as error: self.run_command('issue', 'create', '-t', 'new')
        self.assertEqual(error.exception.details['issue_number'], 2)
        self.assertEqual(error.exception.details['stage'], 'verify')
        self.assertEqual(len(self.writes()), 1)

    def test_ignored_patch_does_not_report_success(self):
        self.client.ignore = True
        with self.assertRaises(GVError) as error: self.run_command('issue', 'close', '1')
        self.assertEqual(error.exception.code, 'verification_failed')

    def test_comments_get_confirmation_and_partial_comment_id(self):
        result = self.run_command('issue', 'comment', '1', '-b', '$HOME\r\nПривет')
        self.assertEqual(result['comment']['body'], '$HOME\r\nПривет')
        self.assertEqual(len(self.run_command('issue', 'comments', '1')['comments']), 1)
        self.client = Fake(); self.client.fail_verify = True
        with self.assertRaises(GVError) as error: self.run_command('issue', 'comment', '1', '-b', 'x')
        self.assertEqual(error.exception.details['comment_id'], 80)

    def test_url_conflicts_and_invalid_selectors(self):
        args = parser().parse_args(['issue', 'view', 'https://gitverse.ru/o/r/issues/12'])
        issue_target(args); self.assertEqual((args.number, args.repo), (12, 'o/r'))
        for value in ('0', '-1', 'https://evil.test/o/r/issues/1', 'https://gitverse.ru/o/r/pulls/1', 'https://gitverse.ru/o/r/issues/1?x=y'):
            args = parser().parse_args(['issue', 'view', value])
            with self.assertRaises(GVError): issue_target(args)
        args = parser().parse_args(['issue', 'view', 'https://gitverse.ru/o/r/issues/1', '-R', 'other/repo'])
        with self.assertRaises(GVError): issue_target(args)

    def test_label_normalizes_color_and_preserves_numeric_names(self):
        result = self.run_command('label', 'create', '123', '-c', '#3aF', '-d', 'Привет')
        self.assertEqual(result['label']['color'], '33aaff')
        self.assertEqual(self.run_command('label', 'view', '123')['name'], '123')
        self.assertEqual(self.writes()[0][3]['description'], 'Привет')

    def test_label_edit_rename_archive_unarchive_and_noop(self):
        result = self.run_command('label', 'edit', 'bug', '-n', 'тест/bug', '--archive', '-c', 'aabbcc')
        self.assertEqual(result['label']['name'], 'тест/bug'); self.assertTrue(result['label']['is_archived'])
        self.assertFalse(self.run_command('label', 'edit', 'тест/bug', '--archive')['changed'])
        self.assertFalse(self.run_command('label', 'edit', 'тест/bug', '--unarchive')['label']['is_archived'])
        self.assertEqual(self.writes()[0][1], 'repos/o/r/labels/1')

    def test_label_validation_duplicate_and_rename_collision(self):
        for args in (('create', 'bug', '-c', 'aaa'), ('create', 'new', '-c', 'bad-color'), ('create', 'new', '-c', 'aaa', '--exclusive'), ('edit', 'bug', '-n', 'keep'), ('edit', 'bug'), ('create', 'x' * 41, '-c', 'aaa')):
            with self.assertRaises(GVError): self.run_command('label', *args)
        self.assertEqual(self.writes(), [])

    def test_label_verify_failure_preserves_created_id(self):
        self.client.fail_verify = True
        with self.assertRaises(GVError) as error: self.run_command('label', 'create', 'new', '-c', 'aaa')
        self.assertEqual(error.exception.details['label_id'], 3)
        self.assertEqual(len(self.writes()), 1)

    def test_label_edit_ignored_and_concurrent_changes_rejected(self):
        self.client.ignore = True
        with self.assertRaises(GVError) as error: self.run_command('label', 'edit', 'bug', '-d', 'new')
        self.assertEqual(error.exception.code, 'verification_failed')
        self.client = Fake(); original = self.client.request
        def changing(method, path, *args, **kwargs):
            if '/labels/' in path and method == 'GET': self.client.labels[0]['description'] = 'Concurrent'
            return original(method, path, *args, **kwargs)
        self.client.request = changing
        with self.assertRaises(GVError) as error: self.run_command('label', 'edit', 'bug', '-d', 'new')
        self.assertEqual(error.exception.code, 'concurrent_change'); self.assertEqual(self.writes(), [])

    def test_dry_run_and_backend_refusal(self):
        self.client.dry_run = True
        self.assertIn('plan', self.run_command('issue', 'create', '-t', 'new', '-a', '@me'))
        self.assertIn('plan', self.run_command('label', 'create', 'new', '-c', 'abc'))
        self.assertEqual(self.client.calls, [])
        with self.assertRaises(GVError): execute(parser().parse_args(['issue', 'list', '-R', 'o/r', '--backend', 'web', '--dry-run']))
