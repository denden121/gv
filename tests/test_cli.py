import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from gitverse_cli.cli import execute, main, parse_repo, parser, repository, unified_patch
from gitverse_cli.client import Client, GVError, NoRedirect, safe_url
from gitverse_cli.discovery import schema, traffic_inventory


class CLITests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def call(self, *argv):
        return execute(parser().parse_args(argv))

    def test_create_multiline_body_and_global_options(self):
        with tempfile.TemporaryDirectory() as directory:
            body = Path(directory) / "body.md"
            body.write_text("Описание\n\n`$HOME` и $(literal)\n")
            result = self.call("--dry-run", "-R", "owner/repo", "pr", "create", "--title", "Изменение", "--head", "feature/one", "--base", "develop", "--draft", "--body-file", str(body))
        self.assertEqual(result["method"], "POST")
        self.assertEqual(result["url"], "https://api.gitverse.ru/repos/owner/repo/pulls")
        self.assertEqual(result["body"], {"title": "Draft: Изменение", "head": "feature/one", "base": "develop", "draft": True, "body": "Описание\n\n`$HOME` и $(literal)\n"})
        contract = schema("/repos/{owner}/{repo}/pulls", "POST")
        allowed = contract["definitions"]["#/definitions/request.CreatePullRequestAPI"]["properties"]
        self.assertFalse(set(result["body"]) - set(allowed))

    def test_review_all_events_and_commit_binding(self):
        for option, event in [("--approve", "APPROVED"), ("--request-changes", "REQUEST_CHANGES"), ("--comment", "COMMENT")]:
            result = self.call("pr", "review", "14", "-R", "o/r", option, "--body", "Ревью", "--commit", "abc", "--dry-run")
            self.assertEqual(result["body"]["event"], event)
            self.assertEqual(result["body"]["commit_id"], "abc")
            self.assertTrue(result["url"].endswith("/pulls/14/reviews"))

    def test_review_automatically_resolves_head_once(self):
        args = parser().parse_args(["pr", "review", "14", "-R", "o/r", "--approve"])
        with patch("gitverse_cli.cli.Client") as factory:
            client = factory.return_value
            client.backend = "public"
            client.dry_run = False
            client.request.side_effect = [{"head": {"sha": "head-sha"}}, {"id": 5, "state": "APPROVED"}]
            self.assertEqual(execute(args), {"id": 5, "state": "APPROVED"})
            self.assertEqual(client.request.call_args_list[1].kwargs["body"]["commit_id"], "head-sha")

    def test_pending_review_is_published_once(self):
        args = parser().parse_args(["pr", "review", "14", "-R", "o/r", "--request-changes", "--body", "Fix", "--commit", "sha"])
        with patch("gitverse_cli.cli.Client") as factory:
            c = factory.return_value; c.backend = "public"; c.dry_run = False
            c.request.side_effect = [{"id": 9, "state": "PENDING"}, {"id": 9, "state": "CHANGES_REQUESTED"}]
            self.assertEqual(execute(args)["state"], "CHANGES_REQUESTED")
            self.assertEqual(c.request.call_count, 2)
            self.assertEqual(c.request.call_args.args, ("POST", "repos/o/r/pulls/14/reviews/9/events"))
            self.assertEqual(c.request.call_args.kwargs["body"], {"event": "REQUEST_CHANGES", "body": "Fix"})

    def test_submit_error_preserves_review_id_and_does_not_repeat(self):
        args = parser().parse_args(["pr", "review", "14", "-R", "o/r", "--approve", "--commit", "sha"])
        with patch("gitverse_cli.cli.Client") as factory:
            c = factory.return_value; c.backend = "public"; c.dry_run = False
            c.request.side_effect = [{"id": 9, "state": "PENDING"}, GVError("Network error", "network_error")]
            with self.assertRaises(GVError) as error:
                execute(args)
            self.assertEqual(error.exception.details["review_id"], 9)
            self.assertEqual(c.request.call_count, 2)

    def test_submit_existing_review_does_not_create_or_read_head(self):
        result = self.call("pr", "review", "14", "-R", "o/r", "--approve", "--review-id", "9", "--dry-run")
        self.assertTrue(result["url"].endswith("/pulls/14/reviews/9/events"))
        self.assertEqual(result["body"], {"event": "APPROVED"})

    def test_unknown_review_state_is_not_reported_as_success(self):
        args = parser().parse_args(["pr", "review", "14", "-R", "o/r", "--approve", "--commit", "sha"])
        with patch("gitverse_cli.cli.Client") as factory:
            c = factory.return_value; c.backend = "public"; c.dry_run = False
            c.request.return_value = {"id": 9}
            with self.assertRaises(GVError) as error:
                execute(args)
            self.assertEqual(error.exception.code, "review_state_unconfirmed")
            self.assertEqual(c.request.call_count, 1)

    def test_review_batch_inline_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "comments.json"
            comments = [{"path": "src/main.py", "body": "Проверь None", "new_position": 42}, {"path": "old.py", "body": "Удаление ломает вызов", "old_position": 8}]
            p.write_text(json.dumps(comments))
            result = self.call("pr", "review", "1", "-R", "o/r", "--comment", "--commit", "sha", "--comments-file", str(p), "--dry-run")
            self.assertNotIn("comments", result["body"])
            self.assertEqual([r["body"]["side"] for r in result["inline_requests_before_review"]], ["RIGHT", "LEFT"])
            self.assertEqual([r["body"]["line"] for r in result["inline_requests_before_review"]], [42, 8])
            p.write_text('[{"path":"a","body":"b","new_position":true}]')
            with self.assertRaises(GVError):
                self.call("pr", "review", "1", "-R", "o/r", "--comment", "--commit", "sha", "--comments-file", str(p), "--dry-run")

    def test_inline_review_partial_failure_preserves_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'comments.json'
            p.write_text(json.dumps([{'path': 'a', 'body': 'one', 'new_position': 1}, {'path': 'a', 'body': 'two', 'new_position': 2}]))
            args = parser().parse_args(['pr', 'review', '1', '-R', 'o/r', '--comment', '--commit', 'sha', '--comments-file', str(p)])
            with patch('gitverse_cli.cli.Client') as factory:
                c = factory.return_value; c.backend = 'public'; c.dry_run = False
                c.request.side_effect = [{'id': 7}, GVError('Network error', 'network_error')]
                with self.assertRaises(GVError) as error:
                    execute(args)
                self.assertEqual(error.exception.details['published_inline_comment_ids'], [7])
                self.assertEqual(error.exception.details['failed_comment_index'], 1)
                self.assertEqual(c.request.call_count, 2)

    def test_patch_can_be_applied_by_git_for_add_modify_remove_and_spaces(self):
        for status, filename, original, hunk in [
            ('added', 'new file.py', None, '@@ -0,0 +1 @@\n+new  '),
            ('modified', 'a.py', 'old\n', '@@ -1 +1 @@\n-old\n+new'),
            ('removed', 'a.py', 'old\n', '@@ -1 +0,0 @@\n-old'),
        ]:
            with tempfile.TemporaryDirectory() as directory:
                if original is not None:
                    (Path(directory) / filename).write_text(original)
                content = unified_patch({'filename': filename, 'previous_filename': None, 'status': status, 'patch': hunk}) + '\n'
                result = subprocess.run(['git', 'apply', '--check', '-'], cwd=directory, input=content, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = subprocess.run(['git', 'apply', '-'], cwd=directory, input=content, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                target = Path(directory) / filename
                if status == 'removed':
                    self.assertFalse(target.exists())
                else:
                    self.assertEqual(target.read_text(), 'new  \n' if status == 'added' else 'new\n')

    def test_plain_inline_reply_are_distinct_endpoints(self):
        base = ["pr", "comment", "3", "-R", "o/r", "--body", "Замечание", "--dry-run"]
        plain = self.call(*base)
        inline = self.call(*base, "--path", "a.py", "--line", "2", "--side", "LEFT", "--commit", "sha")
        reply = self.call(*base, "--reply-to", "55")
        self.assertTrue(plain["url"].endswith("/issues/3/comments"))
        self.assertTrue(inline["url"].endswith("/pulls/3/comments"))
        self.assertEqual(inline["body"]["side"], "LEFT")
        self.assertEqual(reply["body"], {"body": "Замечание", "in_reply_to": 55})

    def test_invalid_inline_is_not_sent(self):
        with patch("gitverse_cli.client.urllib.request.build_opener") as opener:
            with self.assertRaises(GVError):
                self.call("pr", "comment", "3", "-R", "o/r", "--body", "b", "--path", "a.py")
            opener.return_value.open.assert_not_called()

    def test_internal_resolve_uses_website_contract(self):
        result = self.call("pr", "resolve", "4", "9", "-R", "o/r", "--backend", "web", "--dry-run", "--unresolve")
        self.assertEqual(result["method"], "PATCH")
        self.assertTrue(result["url"].endswith("/sbt/api/v1/repos/o/r/pulls/4/comments/9/resolve?action=unresolve"))

    def test_repo_auto_detection_chooses_gitverse_not_origin(self):
        def fake_git(*args):
            if args == ("remote",):
                return "origin\ngithub\nverse"
            return {"origin": "https://example.org/team/repo.git", "github": "git@github.com:owner/repo.git", "verse": "git@gitverse.ru:owner/repo.git"}[args[-1]]
        with patch("gitverse_cli.cli.git", side_effect=fake_git):
            self.assertEqual(repository(parser().parse_args(["pr", "view", "1"])), "owner/repo")

    def test_repository_url_validation(self):
        for value in ("git@gitverse.ru:owner/repo.git", "https://gitverse.ru/owner/repo.git", "ssh://git@gitverse.ru/owner/repo.git", "owner/repo"):
            self.assertEqual(parse_repo(value), "owner/repo")
        for value in ("https://evil.test/o/r", "../repo", "owner/repo/more", "o/r?token=secret"):
            with self.assertRaises(GVError):
                parse_repo(value)

    def test_api_shorthand_and_placeholders(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["api", "repos/{owner}/{repo}/pulls", "-R", "o/r", "-q", "head=feature/foo", "--dry-run"])
        self.assertIn("head=feature%2Ffoo", json.loads(out.getvalue())["url"])

    def test_web_discovery_and_placeholder_names(self):
        records = self.call("api", "endpoints", "--backend", "web", "--search", "reviews/submit")
        self.assertTrue(records)
        self.assertEqual(records[0]["verification"], "static_only")
        result = self.call("api", "request", "repos/{ownerName}/{repoName}/pulls/1/comments", "-R", "o/r", "--backend", "web", "--dry-run")
        self.assertTrue(result["url"].endswith("/repos/o/r/pulls/1/comments"))

    def test_unknown_option_is_json_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as error:
            main(["pr", "review", "--nonsense"])
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(json.loads(err.getvalue())["error"]["code"], "usage_error")

    def test_dry_run_does_not_load_auth_or_network(self):
        os.environ["GITVERSE_TOKEN"] = "SECRET"
        with patch("gitverse_cli.client.load_auth", side_effect=AssertionError), patch("urllib.request.OpenerDirector.open", side_effect=AssertionError):
            result = self.call("pr", "view", "1", "-R", "o/r", "--dry-run")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_no_secret_in_error_body(self):
        client = Client(dry_run=True)
        client.dry_run = False
        client.opener = Mock()
        client.opener.open.side_effect = urllib.error.HTTPError("https://api.gitverse.ru/user", 401, "SECRET", {}, io.BytesIO(b'{"token":"SECRET"}'))
        with self.assertRaises(GVError) as error:
            client.request("GET", "user")
        self.assertNotIn("SECRET", json.dumps(error.exception.as_dict()))


class TransportTests(unittest.TestCase):
    def client(self):
        c = Client(dry_run=True)
        c.dry_run = False
        return c

    def test_pagination_and_envelopes(self):
        c = self.client()
        c.request = Mock(side_effect=[{"reviews": [1, 2]}, {"reviews": [3]}])
        self.assertEqual(c.pages("x", paginate=True, page_size=2, page_key="limit"), [1, 2, 3])
        # Capture copies rather than references to the mutable query dict.
        self.assertEqual(c.request.call_count, 2)

    def test_pagination_is_bounded_and_never_silently_truncates(self):
        c = self.client()
        c.request = Mock(return_value=[1])
        with self.assertRaises(GVError) as error:
            c.pages("x", paginate=True, page_size=1, max_pages=2)
        self.assertEqual(error.exception.code, "pagination_limit")

    def test_unknown_envelope_is_explicit_error(self):
        c = self.client(); c.request = Mock(return_value={"unexpected": []})
        with self.assertRaises(GVError):
            c.pages("x", paginate=True)

    def test_public_token_is_not_used_on_website(self):
        with patch.dict(os.environ, {"GITVERSE_TOKEN": "SECRET"}, clear=True):
            self.assertEqual(Client().headers["Authorization"], "Bearer SECRET")
            self.assertNotIn("Authorization", Client(backend="web").headers)

    def test_rejects_absolute_and_traversal_paths(self):
        for p in ("https://evil.test/x", "//evil.test/x", "../user", "%2e%2e/user", "foo\\bar", "foo#fragment"):
            with self.assertRaises(GVError):
                safe_url("https://api.gitverse.ru", p)

    def test_redirects_are_never_followed(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test"))

    def test_mutation_is_not_retried_even_on_503(self):
        c = self.client(); c.opener = Mock()
        c.opener.open.side_effect = urllib.error.HTTPError("x", 503, "", {}, io.BytesIO())
        with self.assertRaises(GVError):
            c.request("POST", "repos/o/r/pulls", body={"title": "x"})
        self.assertEqual(c.opener.open.call_count, 1)

    def test_get_retries_are_bounded(self):
        c = self.client(); c.opener = Mock()
        c.opener.open.side_effect = [urllib.error.HTTPError("x", 503, "", {}, io.BytesIO()) for _ in range(3)]
        with patch("gitverse_cli.client.time.sleep"), self.assertRaises(GVError):
            c.request("GET", "user")
        self.assertEqual(c.opener.open.call_count, 3)

    def test_har_inventory_discards_credentials_and_values(self):
        har = {"log": {"entries": [{"request": {"url": "https://gitverse.ru/sbt/api/v1/repos/private-owner/private-repo/pulls/42/comments?token=SECRET", "method": "POST", "headers": [{"name": "Cookie", "value": "SECRET"}], "postData": {"text": '{"body":"PRIVATE_TEXT"}'}}, "response": {"status": 200, "content": {"text": '{"id":42,"content":"PRIVATE_TEXT"}'}}}]}}
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "traffic.har"; p.write_text(json.dumps(har))
            result = traffic_inventory([p])
        text = json.dumps(result)
        for secret in ("SECRET", "PRIVATE_TEXT", "private-owner", "private-repo"):
            self.assertNotIn(secret, text)
        self.assertEqual(result["endpoints"][0]["path"], "/repos/{owner}/{repo}/pulls/{id}/comments")


if __name__ == "__main__":
    unittest.main()
