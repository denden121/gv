"""Behavior of PR creation target branch; real local Git, synthetic HTTP DTOs."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from gitverse_cli.pr_create import configured_base
from test_metadata import Public, REPO


class BaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd(); os.chdir(self.tmp.name); self.addCleanup(os.chdir, self.cwd)
        self.env = patch.dict(os.environ, {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        self.c = Public()
        original = self.c.request
        def request(method, path, query=None, body=None, **kwargs):
            if method == 'POST' and path.endswith('/pulls'):
                self.c.pull['base']['ref'] = body['base']
                self.c.pull['head']['ref'] = body['head']
            return original(method, path, query, body, **kwargs)
        self.c.request = request

    def git(self, *args):
        return subprocess.run(['git', *args], capture_output=True, text=True, check=True).stdout

    def init(self):
        self.git('init', '-q', '-b', 'local/current')

    def config(self, value, branch='local/current'):
        self.git('config', f'branch.{branch}.gh-merge-base', value)

    def create(self, *flags):
        args = parser().parse_args(['pr', 'create', '-R', REPO, '--head', 'feature', '--title', 'Created', *flags])
        with patch('gitverse_cli.cli.Client', return_value=self.c):
            return execute(args)

    def test_explicit_nondefault_slash_and_short_alias(self):
        for flag in ('--base', '-B'):
            self.c.calls.clear()
            with patch('gitverse_cli.pr_create.configured_base', side_effect=AssertionError('config')):
                result = self.create(flag, 'release/next', '--body', 'literal', '--draft', '--assignee', '@me')
            self.assertEqual(result['base']['ref'], 'release/next')
            self.assertTrue(result['draft'])
            self.assertEqual(len(self.c.writes()), 1)
            self.assertEqual(self.c.calls[-1][1], f'repos/{REPO}/pulls/8')

    def test_explicit_overrides_config_even_invalid(self):
        self.init(); self.config('invalid..base')
        self.assertEqual(self.create('--base', 'Release/Next')['base']['ref'], 'Release/Next')

    def test_current_config_precedes_default_not_explicit_head_config(self):
        self.init(); self.config('release/current'); self.config('release/head', 'feature')
        self.assertEqual(self.create()['base']['ref'], 'release/current')
        self.assertEqual(len(self.c.writes()), 1)

    def test_default_fallback_inside_and_outside_git(self):
        self.assertEqual(self.create()['base']['ref'], 'main')
        self.init(); self.assertEqual(self.create()['base']['ref'], 'main')

    def test_detached_head_falls_back(self):
        self.init(); self.config('release/current')
        self.git('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '--allow-empty', '-qm', 'Synthetic')
        self.git('checkout', '--detach', '-q')
        self.assertEqual(self.create()['base']['ref'], 'main')

    def test_config_include_and_effective_last_value(self):
        self.init()
        included = Path(self.tmp.name)/'included'
        included.write_text('[branch "local/current"]\n gh-merge-base = release/included\n')
        self.git('config', 'include.path', str(included))
        self.assertEqual(self.create()['base']['ref'], 'release/included')
        self.config('release/last')
        self.assertEqual(self.create()['base']['ref'], 'release/last')

    def test_invalid_explicit_and_config_values_never_write(self):
        self.init()
        for value in ('', ' feature', 'bad..base', 'other:base', 'a\nb', 'a'*1025):
            with self.subTest(value=value[:20]):
                self.config(value)
                for flags in ((), ('--base', value)):
                    with self.assertRaises(GVError): self.create(*flags)
                    self.assertEqual(self.c.calls, [])

    def test_equal_head_base_refuses_before_network(self):
        self.init(); self.config('feature')
        for flags in ((), ('-B', 'feature')):
            with self.assertRaises(GVError): self.create(*flags)
        self.assertEqual(self.c.calls, [])

    def test_invalid_default_dto_refuses_no_write(self):
        original = self.c.request
        for value in (None, [], '', 'bad..base'):
            self.c.request = lambda m,p,q=None,body=None,**kw: {**original(m,p,q,body,**kw), 'default_branch':value}
            with self.assertRaises(GVError): self.create()
            self.assertEqual(self.c.writes(), [])

    def test_offline_explicit_or_config_no_network_or_credentials(self):
        self.c = Client(dry_run=True)
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')), patch('builtins.input', side_effect=AssertionError('prompt')):
            result = self.create('-B', 'release/next', '--dry-run')
            self.assertEqual(result['body']['base'], 'release/next')
            self.init(); self.config('release/config')
            result = self.create('--dry-run')
            self.assertEqual(result['body']['base'], 'release/config')
            self.assertNotIn('number', result)

    def test_offline_no_config_does_not_lookup_default(self):
        self.c = Client(dry_run=True)
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')):
            with self.assertRaises(GVError) as caught: self.create('--dry-run')
        self.assertEqual(caught.exception.code, 'usage_error')

    def test_base_dto_scope_ref_sha_and_concurrent_readback(self):
        original = self.c.request
        for stage in ('POST', 'GET'):
            for change in ('wrong_ref', 'wrong_repo', 'wrong_id', 'bool_id', 'missing_sha', 'changed_sha'):
                with self.subTest(stage=stage, change=change):
                    self.c.calls.clear()
                    def request(m,p,q=None,body=None,**kw):
                        row = original(m,p,q,body,**kw)
                        if (m=='POST' if stage=='POST' else p.endswith('/pulls/8')):
                            row = copy.deepcopy(row)
                            if change=='wrong_ref': row['base']['ref']='other'
                            if change=='wrong_repo': row['base']['repo']['full_name']='foreign/repo'
                            if change=='wrong_id': row['base']['repo']['id']=8
                            if change=='bool_id': row['base']['repo_id']=True
                            if change=='missing_sha': row['base']['sha']=None
                            if change=='changed_sha': row['base']['sha']='c'*40
                        return row
                    self.c.request=request
                    # A well-formed changed SHA on POST is legitimate; mismatch is detected on GET.
                    if stage=='POST' and change=='changed_sha': continue
                    with self.assertRaises(GVError) as caught: self.create('-B','release/next')
                    self.assertEqual(caught.exception.details['pull_number'],8)
                    self.assertEqual(caught.exception.details['base'],'release/next')
                    self.assertEqual(len(self.c.writes()),1)

    def test_denied_or_missing_base_server_refusal_no_retry(self):
        original = self.c.request
        for status in (400,401,403,404):
            self.c.calls.clear()
            def request(m,p,q=None,body=None,**kw):
                row=original(m,p,q,body,**kw)
                if m=='POST': raise GVError('Denied synthetic', 'http_error',status)
                return row
            self.c.request=request
            with self.assertRaises(GVError) as caught: self.create('-B','missing/base')
            self.assertEqual(caught.exception.details['base'],'missing/base')
            self.assertTrue(caught.exception.details['mutation_attempted'])
            self.assertEqual(len(self.c.writes()),1)

    def test_local_config_deadline_failure_is_explicit(self):
        with self.assertRaises(GVError) as caught: configured_base(time.monotonic()-1)
        self.assertEqual(caught.exception.code,'deadline_exceeded')

    def test_local_config_git_error_does_not_fallback(self):
        self.init(); Path('.git/config').write_text('invalid config\n')
        with self.assertRaises(GVError) as caught: self.create()
        self.assertEqual(caught.exception.code,'git_error')
        self.assertEqual(self.c.calls,[])

    def test_discovery(self):
        args=parser().parse_args(['agent-info'])
        result=execute(args)
        self.assertEqual(result['pr_create_base']['flags'],['--base','-B'])
