"""Label creation contracts; synthetic transports never count as live evidence."""
import copy
import os
import unittest
from unittest.mock import Mock, patch
import urllib.error

from gitverse_cli.client import Client, GVError
from gitverse_cli.metadata import label_key
import test_pr_create as core


class LabelFlagTests(unittest.TestCase):
    setUp = core.CreateTests.setUp
    create = core.CreateTests.create
    intercept = core.CreateTests.intercept

    def test_names_repeated_comma_case_dedup_and_public_only(self):
        with patch('gitverse_cli.metadata.web_client', side_effect=AssertionError('cookies')):
            result = self.create('--label', 'TWO,one', '-l', 'One,two')
        self.assertEqual(self.c.writes()[0][3]['labels'], [12, 11])
        self.assertEqual({r['id'] for r in result['labels']}, {11, 12})
        self.assertEqual(len(self.c.writes()), 1)
        self.assertEqual(self.c.reads, 2)

    def test_space_unicode_and_numeric_names(self):
        for name in ('help wanted', 'Ошибка', '123', 'scope/one'):
            self.c = core.Public()
            self.c.labels[0]['name'] = name
            result = self.create('-l', name.upper())
            self.assertEqual(result['labels'][0]['name'], name)

    def test_simple_unicode_fold_does_not_expand_names(self):
        self.assertEqual(label_key('ΣςσKK'), label_key('σσσkk'))
        self.assertNotEqual(label_key('ß'), label_key('ss'))
        self.assertNotEqual(label_key('İ'), label_key('i'))

    def test_invalid_inputs_offline_before_network(self):
        for value in ('', 'a,,b', 'bad\nname', 'bad\x7fname', 'x'*41, '\ud800', ','.join(str(i) for i in range(101))):
            with self.subTest(value=repr(value[:20])):
                with self.assertRaises(GVError): self.create('-l', value)
                self.assertEqual(self.c.calls, [])

    def test_unknown_archived_exclusive_refuse_without_write(self):
        for mode in ('unknown', 'archived', 'exclusive'):
            self.c = core.Public()
            if mode == 'archived': self.c.labels[0]['is_archived'] = True
            if mode == 'exclusive':
                for i, row in enumerate(self.c.labels): row.update(name='scope/'+str(i), exclusive=True)
            with self.assertRaises(GVError):
                self.create('-l', 'missing' if mode == 'unknown' else 'one' if mode == 'archived' else 'scope/0,scope/1')
            self.assertEqual(self.c.writes(), [])

    def test_malformed_catalog_and_ambiguous_names_refuse(self):
        for rows in (None, {}, [{'id': True, 'name': 'one'}], [{'id': 1, 'name': ''}],
                     [{'id': 1, 'name': 'one'}],
                     [{'id': 1, 'name': 'one', 'exclusive': 'false', 'is_archived': False}],
                     [{'id': 11, 'name': 'ONE', 'exclusive': False, 'is_archived': False},
                      {'id': 12, 'name': 'one', 'exclusive': False, 'is_archived': False}]):
            self.c = core.Public()
            self.intercept(lambda m,p,r,k: rows if p.endswith('/labels') else r)
            with self.assertRaises(GVError): self.create('-l', 'one')
            self.assertEqual(self.c.writes(), [])

    def test_short_pages_continue_to_empty_twice(self):
        def hook(m,p,r,k):
            if p.endswith('/labels'):
                page = self.c.calls[-1][2]['page']
                return copy.deepcopy(self.c.labels[page-1:page] if page <= 2 else [])
            return r
        self.intercept(hook)
        self.create('-l', 'two')
        self.assertEqual([c[2]['page'] for c in self.c.calls if c[1].endswith('/labels')], [1,2,3,1,2,3])

    def test_oversized_repeated_and_bounded_catalog_refuse(self):
        for mode, code in (('oversized', 'pagination_limit'), ('repeat', 'pagination_error'), ('bound', 'pagination_limit')):
            self.c = core.Public()
            def hook(m,p,r,k):
                if not p.endswith('/labels'): return r
                page = self.c.calls[-1][2]['page']
                if mode == 'repeat': return [self.c.labels[0]]
                return [{'id': i, 'name': str(i), 'exclusive': False, 'is_archived': False}
                        for i in (range(1,102) if mode == 'oversized' else [page])]
            self.intercept(hook)
            with self.assertRaises(GVError) as caught: self.create('-l', 'one')
            self.assertEqual(caught.exception.code, code)
            self.assertFalse(caught.exception.details['mutation_attempted'])
            self.assertEqual(self.c.writes(), [])

    def test_full_page_requires_empty_page(self):
        self.c.labels = [{'id': i, 'name': str(i), 'exclusive': False, 'is_archived': False} for i in range(1,101)]
        self.create('-l', '100')
        self.assertEqual([c[2]['page'] for c in self.c.calls if c[1].endswith('/labels')], [1,2,1,2])

    def test_preflight_label_identity_or_state_change_no_write(self):
        for field,value in (('id', 99), ('name', 'ONE'), ('exclusive', True), ('is_archived', True)):
            self.c = core.Public(); scans = 0
            def hook(m,p,r,k):
                nonlocal scans
                if p.endswith('/labels') and self.c.calls[-1][2]['page'] == 1:
                    scans += 1
                    if scans == 2: r[0][field] = value
                return r
            self.intercept(hook)
            with self.assertRaises(GVError): self.create('-l', 'one')
            self.assertEqual(self.c.writes(), [])

    def test_wrong_scope_and_identity_no_write(self):
        self.intercept(lambda m,p,r,k: {**r, 'full_name':'other/repo'} if p == 'repos/owner/repo' else r)
        with self.assertRaises(GVError): self.create('-l', 'one')
        self.assertEqual(self.c.writes(), [])

    def test_denied_catalog_and_missing_permissions_no_retry(self):
        for target in ('catalog', 'post'):
            for status in (401,403,404):
                self.c = core.Public()
                def hook(m,p,r,k):
                    if (target == 'catalog' and p.endswith('/labels')) or (target == 'post' and m == 'POST'):
                        raise GVError('denied', 'http_error', status)
                    return r
                self.intercept(hook)
                with self.assertRaises(GVError) as caught: self.create('-l', 'one')
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(len(self.c.writes()), int(target == 'post'))

    def test_malformed_wrong_id_duplicate_missing_extra_readbacks(self):
        for rows in (None, {}, [], [{'id':99,'name':'one'}], [{'id':11,'name':'other'}],
                     [{'id':True,'name':'one'}], [{'id':11}],
                     [{'id':11,'name':'one'}]*2,
                     [{'id':11,'name':'one'},{'id':12,'name':'two'}]):
            self.c = core.Public()
            self.c.on_read = lambda c,n: c.pull.update(labels=copy.deepcopy(rows))
            with self.assertRaises(GVError) as caught: self.create('-l', 'one')
            self.assertEqual(caught.exception.details['pull_id'], 88)
            self.assertEqual(caught.exception.details['mutation_outcome'], 'confirmed')
            self.assertEqual(len(self.c.writes()), 1)

    def test_wrong_pr_metadata_id_refuses(self):
        self.c.on_read = lambda c,n: c.pull.update(id=89)
        with self.assertRaises(GVError): self.create('-l', 'one')
        self.assertEqual(len(self.c.writes()), 1)

    def test_final_read_concurrent_label_change_preserves_recovery(self):
        self.c.on_read = lambda c,n: c.pull.update(labels=[]) if n == 2 else None
        with self.assertRaises(GVError) as caught: self.create('-l', 'one')
        self.assertEqual(caught.exception.code, 'concurrent_change')
        self.assertEqual(caught.exception.details['pull_number'], 8)
        self.assertEqual(len(self.c.writes()), 1)

    def test_unknown_write_and_readback_never_retry(self):
        for target in ('repos/owner/repo/pulls', 'repos/owner/repo/pulls/8'):
            self.c = core.Public()
            def hook(m,p,r,k):
                if p == target: raise GVError('unknown', 'network_error')
                return r
            self.intercept(hook)
            with self.assertRaises(GVError) as caught: self.create('-l', 'one')
            self.assertEqual(len(self.c.writes()), 1)
            self.assertEqual(caught.exception.details['mutation_outcome'], 'unknown' if target.endswith('/pulls') else 'confirmed')

    def test_identical_names_dedup_but_each_invocation_creates(self):
        self.create('-l', 'one,ONE,one'); self.create('-l', 'one')
        self.assertEqual([c[3]['labels'] for c in self.c.writes()], [[11],[11]])

    def test_draft_body_and_assignee_combination(self):
        row = self.create('-l','ONE,two','-a','@me','--draft','-b','literal\r\nbody')
        self.assertTrue(row['draft'])
        self.assertEqual(row['body'], 'literal\r\nbody')
        self.assertEqual(row['assignees'][0]['id'], 1)
        self.assertEqual({r['id'] for r in row['labels']}, {11,12})

    def test_bounded_complete_transport_and_single_deadline(self):
        options=[]
        self.intercept(lambda m,p,r,k: options.append(k) or r)
        self.create('-l', 'one')
        self.assertEqual(len({k['deadline'] for k in options}), 1)
        self.assertTrue(all(k['require_complete'] and k['max_response_bytes']==33554432 for k in options))
        for code in ('deadline_exceeded','response_limit','incomplete_response'):
            self.c = core.Public()
            def hook(m,p,r,k):
                if p.endswith('/labels'): raise GVError('bound',code)
                return r
            self.intercept(hook)
            with self.assertRaises(GVError): self.create('-l', 'one')
            self.assertEqual(self.c.writes(), [])

    def test_offline_unresolved_names_no_network_cookies_or_auth(self):
        self.c = Client(dry_run=True)
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')), patch('builtins.input', side_effect=AssertionError('prompt')), patch('gitverse_cli.client.load_auth', side_effect=AssertionError('auth')), patch('gitverse_cli.metadata.web_client', side_effect=AssertionError('cookies')):
            row = self.create('-l','ONE,two','--dry-run')
        self.assertEqual(row['unresolved_metadata']['labels'], ['ONE','two'])
        self.assertNotIn('labels', row['base_request_template']['body'])
        self.assertFalse(row['validation']['final_request_known'])
        self.assertEqual(row['network_requests'], 0)

    def test_label_lookup_redirect_never_forwards_credentials(self):
        with patch.dict(os.environ, {'GITVERSE_TOKEN':'synthetic-only-token'}): self.c=Client()
        self.c.opener=Mock()
        self.c.opener.open.side_effect=urllib.error.HTTPError('https://api.gitverse.ru/repos/owner/repo/labels',302,'redirect',{'Location':'https://foreign.invalid/'},None)
        with self.assertRaises(GVError): self.create('-l','one')
        self.assertEqual(self.c.opener.open.call_count,1)
        req=self.c.opener.open.call_args.args[0]
        self.assertTrue(req.full_url.startswith('https://api.gitverse.ru/repos/owner/repo/labels?'))
        self.assertEqual(req.get_header('Authorization'),'Bearer synthetic-only-token')
        self.assertIsNone(req.get_header('Cookie'))
