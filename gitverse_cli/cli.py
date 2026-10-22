import argparse
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import __version__
from .client import Client, GVError, config_path, validate_base
from .discovery import endpoints, schema, traffic_inventory


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise GVError(message, "usage_error")

    def parse_args(self, args=None, namespace=None):
        raw_args = sys.argv[1:] if args is None else args
        try:
            parsed, extra = self.parse_known_args(args, namespace)
        except GVError as error:
            if error.code == 'usage_error' and any(word in ('secret', 'variable') for word in raw_args):
                raise GVError('Invalid Actions config arguments; pass values through stdin or --value-file', 'usage_error') from error
            raise
        if getattr(parsed, 'group', None) == 'pr' and getattr(parsed, 'action', None) == 'resolve':
            # argparse stops a variable positional list at intervening flags.
            # Preserve both "resolve PR -R REPO COMMENT" and current-PR form.
            if any(value.startswith('-') for value in extra):
                self.error('unrecognized arguments: ' + ' '.join(extra))
            targets = parsed.targets + extra
            if len(targets) not in (1, 2):
                self.error('resolve requires [PR_SELECTOR] COMMENT_ID')
            parsed.selector = targets[0] if len(targets) == 2 else None
            try:
                parsed.comment_id = positive(targets[-1])
            except (ValueError, argparse.ArgumentTypeError):
                self.error('comment_id must be a positive integer')
            del parsed.targets
        elif extra:
            if getattr(parsed, 'group', None) in ('secret', 'variable'):
                self.error('Unrecognized Actions config arguments; pass values through stdin or --value-file')
            self.error('unrecognized arguments: ' + ' '.join(extra))
        return parsed


def git(*args, cwd=None):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise GVError("Git command failed: " + " ".join(args[:2]), "git_error")
    return p.stdout.strip()


def parse_repo(value, host="gitverse.ru"):
    value = value.strip()
    if "://" in value:
        p = urlsplit(value)
        if p.hostname != host:
            raise GVError("Repository URL belongs to a different host", "invalid_repo")
        value = p.path.strip("/")
    elif re.match(r"[^/]+@[^:]+:", value):
        ssh_host, value = value.split(":", 1)
        if ssh_host.split("@")[-1] != host:
            raise GVError("Git remote belongs to a different host", "invalid_repo")
    value = value.removesuffix(".git")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", value, flags=re.ASCII) or any(x in (".", "..") for x in value.split("/")):
        raise GVError("Repository must be OWNER/REPO or a GitVerse Git URL", "invalid_repo")
    return value


def repository(args):
    host = os.environ.get("GV_GIT_HOST", "gitverse.ru")
    if getattr(args, "repo", None) or os.getenv("GV_REPO"):
        return parse_repo(getattr(args, "repo", None) or os.environ["GV_REPO"], host)
    try:
        remotes = git("remote").splitlines()
    except (GVError, FileNotFoundError):
        # A saved fallback must not hide a broken or unreadable checkout.
        cwd = Path.cwd()
        if not any(key in os.environ for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR')) and not any(os.path.lexists(parent / '.git') for parent in (cwd, *cwd.parents)):
            from .settings import load_settings
            default = load_settings()['config'].get('default_repo')
            if default: return default
        raise
    matches = {}
    for remote in remotes:
        try:
            matches[remote] = parse_repo(git("remote", "get-url", remote), host)
        except GVError:
            pass
    if not matches:
        raise GVError("No GitVerse remote found; pass -R OWNER/REPO", "repo_required")
    from .repo_default import read_default, selected_remote
    selected = read_default()
    if selected is not None:
        return selected_remote(selected, matches)
    if len(set(matches.values())) > 1:
        raise GVError("Several GitVerse repositories found; pass -R OWNER/REPO", "ambiguous_repo")
    return next(iter(matches.values()))


def read_json(name):
    return json.loads(sys.stdin.read() if name == "-" else Path(name).read_text())


def body_text(args, required=False):
    if getattr(args, "body_file", None) is not None:
        if args.body_file == '-':
            result = sys.stdin.read()
        else:
            with Path(args.body_file).open(encoding='utf-8', newline='') as source:
                result = source.read()
    else:
        result = getattr(args, "body", None)
    if required and not (result and result.strip()):
        raise GVError("A non-empty --body or --body-file is required", "usage_error")
    return result


def fields(values):
    result = {}
    for item in values or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise GVError("Fields must be KEY=VALUE", "usage_error")
        result[key] = value
    return result


def require_public(client, action):
    if client.backend != "public":
        raise GVError(f"{action} uses the documented public API; use --backend public and GITVERSE_TOKEN", "backend_unsupported")


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def duration(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError('must be a finite positive number of seconds')
    return seconds


def parser():
    common = Parser(add_help=False)
    common.add_argument("-R", "--repo", default=argparse.SUPPRESS, help="OWNER/REPO; otherwise use GitVerse remote, or saved default_repo outside Git")
    common.add_argument("--backend", choices=["public", "web"], default=argparse.SUPPRESS)
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS, help="Print request without sending it")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON is the default output")
    common.add_argument("--fields", default=argparse.SUPPRESS, help="Select comma-separated top-level JSON fields")
    root = Parser(prog="gv", description="GitVerse for agents. JSON stdout, JSON errors on stderr. No prompts.", parents=[common])
    root.add_argument("--version", action="version", version=__version__)
    groups = root.add_subparsers(dest="group", required=True)

    def group(name, description):
        g = groups.add_parser(name, help=description, parents=[common])
        return g.add_subparsers(dest="action", required=True)

    def command(g, name, help_text):
        if name == 'checks':
            # A separate parent avoids changing the shared boolean --json action.
            checks_common = Parser(add_help=False)
            for action in common._actions:
                if '--json' not in action.option_strings:
                    checks_common._add_action(action)
            checks_common.add_argument('--json', nargs='?', const=True, default=argparse.SUPPRESS,
                                       metavar='FIELDS', help='Check fields: bucket,completedAt,description,event,link,name,startedAt,state,workflow; bare flag keeps envelope')
            return g.add_parser(name, help=help_text, parents=[checks_common])
        return g.add_parser(name, help=help_text, parents=[common])

    def body(p):
        b = p.add_mutually_exclusive_group()
        b.add_argument("-b", "--body")
        b.add_argument("-F", "--body-file", help="UTF-8 file, or - for stdin")

    def number(p):
        p.add_argument('selector', nargs='?', help='PR number, GitVerse URL, head branch or OWNER[:REPO]:BRANCH; defaults to current Git branch')

    def listing(p):
        p.add_argument("--limit", type=positive, default=30)
        p.add_argument("--page", type=positive, default=1)
        p.add_argument("--paginate", action="store_true", help="Collect all pages, bounded to 100 pages")

    org = group('org', 'Read organization profiles, repositories and membership')
    team = group('team', 'Find visible organization teams, members and repositories')
    for commands, actions in ((org, ('view', 'repos', 'member')), (team, ('list', 'view', 'members', 'repos'))):
        for action in actions:
            p = command(commands, action, 'Read ' + action + ' through public API')
            if commands is org:
                p.add_argument('organization', help='Organization login')
                if action == 'member': p.add_argument('username', help='User login or @me')
            elif action == 'list':
                p.add_argument('--org', required=True, help='Organization login')
            else:
                p.add_argument('team', help='ORG/SLUG; resolve numeric team ID from the result')
            p.add_argument('--timeout', type=duration, default=60)
            p.add_argument('--max-pages', type=positive, default=100)
            if action in ('list', 'repos', 'members'): listing(p)
            if commands is team and action == 'members':
                p.add_argument('--role', choices=('member', 'maintainer'), help='Default includes all roles and child-team members')

    config = group('config', 'Manage local settings separately from authentication')
    for action in ('list', 'get', 'set', 'unset', 'path'):
        p = command(config, action, 'Config ' + action)
        if action in ('get', 'set', 'unset'): p.add_argument('key', help='default_repo or http_timeout')
        if action == 'set': p.add_argument('value')
        if action in ('list', 'get'): p.add_argument('--effective', action='store_true', help='Include environment overrides; does not resolve current Git context')
    aliases = group('alias', 'Manage and expand local argv aliases without a shell')
    for action in ('list', 'set', 'delete', 'import'):
        p = command(aliases, action, 'Alias ' + action)
        if action == 'set':
            p.add_argument('name')
            p.add_argument('expansion', nargs='?', help='Quoted gv argv without gv prefix; standalone $1..$99 or $@')
            p.add_argument('--argv-file', help='JSON argv array from a regular UTF-8 file or - for stdin')
        if action == 'import': p.add_argument('file', nargs='?', default='-', help='JSON alias map or alias list output; default stdin')
        if action in ('set', 'import'):
            p.add_argument('--clobber', action='store_true', help='Explicitly replace conflicting saved definitions')
            p.add_argument('--timeout', type=duration, default=60, help='JSON input deadline')
        if action == 'delete':
            p.add_argument('name', nargs='?')
            p.add_argument('--all', action='store_true')

    for kind in ('secret', 'variable'):
        config = group(kind, 'Manage Actions ' + kind + 's through public PAT API')
        for action in ('list', 'get', 'set', 'delete'):
            p = command(config, action, kind.capitalize() + ' ' + action)
            p.add_argument('--org', help='Organization scope instead of repository; cannot combine with -R')
            p.add_argument('--timeout', type=duration, default=60)
            p.add_argument('--max-pages', type=positive, default=100)
            if action == 'list':
                listing(p)
                if kind == 'variable': p.add_argument('--show-values', action='store_true', help='Explicitly include variable values in list output')
            else: p.add_argument('name', help='Uppercase ASCII Actions name: [A-Z_][A-Z0-9_]*')
            if action == 'set':
                p.add_argument('--value-file', help='UTF-8 regular file or -; default reads piped stdin, preserving exact newlines')
                p.add_argument('--max-bytes', type=positive, default=65536, help='Input byte bound; server may impose a lower limit')

    release = group('release', 'Manage releases and individual assets through public PAT API')
    for action in ('list', 'view', 'create', 'edit', 'delete', 'assets', 'upload', 'download', 'delete-asset'):
        p = command(release, action, 'Release ' + action)
        p.add_argument('--timeout', type=duration, default=300)
        p.add_argument('--max-pages', type=positive, default=100)
        if action == 'list':
            listing(p)
            p.add_argument('--exclude-drafts', action='store_true')
            p.add_argument('--exclude-pre-releases', action='store_true')
        else: p.add_argument('tag', help='Exact tag, including numeric/slash names; no latest inference')
        if action in ('create', 'edit'):
            p.add_argument('-t', '--title')
            notes = p.add_mutually_exclusive_group()
            notes.add_argument('-n', '--notes', dest='body')
            notes.add_argument('-F', '--notes-file', dest='body_file', help='UTF-8 file or - for stdin')
            draft = p.add_mutually_exclusive_group()
            draft.add_argument('--draft', dest='draft', action='store_true', default=None)
            draft.add_argument('--publish', dest='draft', action='store_false')
            pre = p.add_mutually_exclusive_group()
            pre.add_argument('--prerelease', dest='prerelease', action='store_true', default=None)
            pre.add_argument('--no-prerelease', dest='prerelease', action='store_false')
            auth = p.add_mutually_exclusive_group()
            auth.add_argument('--authorized-only', dest='is_authorized_only', action='store_true', default=None)
            auth.add_argument('--no-authorized-only', dest='is_authorized_only', action='store_false')
            if action == 'create': p.add_argument('--target', required=True, help='Branch/tag/full commit SHA resolved before creating the release/tag')
        if action == 'upload':
            p.add_argument('file', help='One regular local file; symlinks refused')
            p.add_argument('--name', help='Asset name, defaults to file basename')
        if action == 'download':
            p.add_argument('--name', required=True, help='One exact asset name')
            p.add_argument('-o', '--output', required=True, help='New local file; existing paths refused')
        if action == 'delete-asset': p.add_argument('name', help='One exact asset name')
        if action in ('upload', 'download'):
            p.add_argument('--max-bytes', type=positive, default=67108864, help='64 MiB file bound')

    issue = group('issue', 'Inspect and manage repository issues through public PAT API')
    for action in ('list', 'view', 'create', 'edit', 'close', 'reopen', 'comment', 'comments'):
        p = command(issue, action, 'Issue ' + action)
        p.add_argument('--timeout', type=duration, default=60)
        p.add_argument('--max-pages', type=positive, default=100)
        if action not in ('list', 'create'):
            p.add_argument('selector', help='Issue number or canonical GitVerse issue URL; PRs are refused')
        if action == 'list':
            listing(p)
            p.add_argument('--state', choices=('open', 'closed', 'all'), default='open')
            p.add_argument('-A', '--author')
            p.add_argument('-a', '--assignee')
            p.add_argument('-l', '--label', action='append')
            p.add_argument('--search-text', help='Literal case-insensitive title/body substring')
        if action in ('create', 'edit'):
            p.add_argument('-t', '--title', required=action == 'create')
            body(p)
            if action == 'create':
                p.add_argument('-a', '--assignee', action='append')
                p.add_argument('-l', '--label', action='append')
            else:
                for kind in ('assignee', 'label'):
                    for direction in ('add', 'remove'):
                        p.add_argument('--' + direction + '-' + kind, action='append')
        if action == 'comment': body(p)

    p = command(issue, 'comment-edit', 'Edit one own discussion comment by explicit ID')
    p.add_argument('selector', help='Issue number or canonical issue URL')
    p.add_argument('--comment-id', type=positive, required=True)
    body(p)
    p.add_argument('--timeout', type=duration, default=60)

    label = group('label', 'Manage repository labels through public PAT API')
    for action in ('list', 'view', 'create', 'edit'):
        p = command(label, action, 'Label ' + action)
        p.add_argument('--timeout', type=duration, default=60)
        p.add_argument('--max-pages', type=positive, default=100)
        if action == 'list': listing(p)
        else: p.add_argument('name', help='Exact label name; numeric names are resolved by name')
        if action in ('create', 'edit'):
            p.add_argument('-c', '--color', required=action == 'create', help='3/6 hex digits, optional #')
            p.add_argument('-d', '--description')
            if action == 'edit': p.add_argument('-n', '--new-name')
            exclusive = p.add_mutually_exclusive_group()
            exclusive.add_argument('--exclusive', dest='exclusive', action='store_true', default=None)
            exclusive.add_argument('--no-exclusive', dest='exclusive', action='store_false')
            archived = p.add_mutually_exclusive_group()
            archived.add_argument('--archive', dest='is_archived', action='store_true', default=None)
            archived.add_argument('--unarchive', dest='is_archived', action='store_false')

    search = group('search', 'Literal scoped search through public PAT API')
    for action in ('repos', 'code', 'commits', 'prs', 'issues'):
        p = command(search, action, {'repos': 'Search owned repositories', 'code': 'Search UTF-8 file lines in one pinned repository', 'commits': 'Search commit messages in complete pinned repository history', 'prs': 'Search PR titles/bodies across explicit repository scopes', 'issues': 'Search issue titles/bodies across explicit repository scopes'}[action])
        p.add_argument('query', help='Literal substring; no regex or GitHub query syntax')
        p.add_argument('--case-sensitive', action='store_true')
        p.add_argument('--timeout', type=duration, default=120)
        p.add_argument('--max-pages', type=positive, default=100)
        p.add_argument('--page-size', type=positive, default=100)
        p.add_argument('--max-entries', type=positive, default=100000)
        if action == 'repos':
            p.add_argument('--owner', default='@me', help='@me includes owned private repositories; another user lists public repositories only')
        elif action == 'code':
            p.add_argument('--ref', help='Branch, tag or full commit SHA')
            p.add_argument('--path', default='', help='Restrict to one file or directory')
            p.add_argument('--max-files', type=positive, default=1000)
            p.add_argument('--max-bytes', type=positive, default=10485760, help='Maximum bytes per file')
            p.add_argument('--max-total-bytes', type=positive, default=52428800)
            p.add_argument('--max-matches', type=positive, default=10000, help='Fail instead of truncating matched lines')
        elif action == 'commits':
            p.set_defaults(page_size=50)
            p.add_argument('--ref', help='Branch/tag/full SHA; default repository branch, resolved and pinned')
            p.add_argument('--max-matches', type=positive, default=10000, help='Fail instead of truncating matched commits')
        else:
            p.add_argument('--repos', action='append', help='Explicit OWNER/REPO scope; repeat for multiple repos; cannot combine with -R')
            p.add_argument('--max-repos', type=positive, default=20, help='Maximum selected scopes, at most 100')
            p.add_argument('--max-matches', type=positive, default=10000)
            p.add_argument('--state', choices=('open', 'closed', 'all', 'merged') if action == 'prs' else ('open', 'closed', 'all'), default='all')
            p.add_argument('--author', help='Exact login or @me')
            p.add_argument('--assignee', help='Exact login or @me')
            p.add_argument('--label', action='append', help='All requested labels must match; comma-separated/repeatable')

    pr = group("pr", "Create, inspect and review pull requests")
    p = command(pr, "checkout", "Fetch PR head and safely switch the local working tree")
    number(p)
    checkout_mode = p.add_mutually_exclusive_group()
    checkout_mode.add_argument("-b", "--branch", help="Local branch name (nonempty Git branch); defaults to PR head.ref; incompatible with --detach")
    checkout_mode.add_argument("--detach", action="store_true", help="Checkout the reviewed SHA without a local branch")
    p.add_argument('-f', '--force', action='store_true', help='Reset the target local branch to the PR SHA; clean tree required; ignored with --detach')
    p.add_argument('--worktree', metavar='NEW_PATH', help='Create a linked worktree at a nonempty new path; verify original HEAD is unchanged')
    p.add_argument('--timeout', type=duration, default=300, help='Checkout Git/API budget after selector resolution, in seconds')
    p = command(pr, 'checks', 'Inspect CI for the exact PR head; unknown merge requirements never count as success')
    number(p)
    p.add_argument('--ci-only', action='store_true', help='Explicitly restrict exit status to visible CI in the base repo')
    p.add_argument('--watch', action='store_true', help='Wait for pending CI or the first run to appear')
    p.add_argument('--interval', type=duration, default=10, help='Polling interval in seconds (default 10)')
    p.add_argument('--timeout', type=duration, default=300, help='Overall request/watch budget in seconds (default 300)')
    p.add_argument('--max-pages', type=positive, default=20, help='Maximum pages per runs/jobs list; truncation prevents success')
    p.add_argument('--max-entries', type=positive, default=100000, help='Combined run/job entry limit per poll; responses capped at 1 MiB each')
    p.add_argument('--fail-fast', action='store_true', help='While watching, stop on the first observed failure/cancellation')
    p.add_argument('--required', action='store_true', help='Report unsupported required-check coverage explicitly')
    p = command(pr, 'ready', 'Mark an open PR ready for review; --undo converts it to draft')
    number(p)
    p.add_argument('--undo', action='store_true', help='Convert to draft by adding the GitVerse Draft: title prefix')
    p = command(pr, 'status', 'Open PRs for the current branch, created by you, and requesting your review')
    p.add_argument('--max-pages', type=positive, default=100, help='PR/CI pagination bound; incomplete scans fail')
    p.add_argument('--checks', action='store_true', help='Also load visible CI for relevant PRs; external/required checks remain unknown')
    p.add_argument('--checks-limit', type=positive, default=20, help='Refuse CI enrichment above this relevant PR count')
    p.add_argument('--checks-timeout', type=duration, default=30, help='CI request budget per PR in seconds')
    p = command(pr, "list", "List pull requests"); listing(p)
    p.add_argument("--state", choices=["open", "closed", "merged", "all"], default="open")
    p.add_argument("--head"); p.add_argument("--base")
    p.add_argument('-A', '--author', help='Exact author login, or @me')
    p.add_argument('-a', '--assignee', help='Exact assignee login, or @me')
    p.add_argument('-l', '--label', action='append', help='Label names; repeat or separate with commas; all must match')
    draft_filter = p.add_mutually_exclusive_group()
    draft_filter.add_argument('--draft', action='store_true', help='Only draft PRs')
    draft_filter.add_argument('--no-draft', action='store_true', help='Exclude draft PRs')
    p.add_argument('--reviewer', help='Exact directly requested reviewer login, or @me; team membership is not inferred')
    p.add_argument('--search-text', help='Literal case-insensitive substring in title/body; no GitHub search syntax')
    p.add_argument('--max-pages', type=positive, default=100, help='Complete scan bound for local filters')
    for action, help_text in [("view", "PR details"), ("files", "Changed files with patches"), ("commits", "PR commits"), ("reviews", "Submitted reviews"), ("comments", "Discussion comments"), ("threads", "Rich discussion threads from website API"), ("context", "Collect PR, files, reviews, comments and commits"), ("diff", "Changed file patches for review"), ("close", "Close PR"), ("reopen", "Reopen PR"), ("update-branch", "Update PR branch")]:
        p = command(pr, action, help_text); number(p)
        if action == 'close':
            p.add_argument('-c', '--comment', help='Closing discussion comment; published once before close')
            p.add_argument('--timeout', type=duration, default=60)
        if action in ("files", "commits", "reviews", "comments", "threads"):
            listing(p)
        if action == "diff":
            p.add_argument("--patch", action="store_true", help="Emit unified diff text, fail if any patch is missing")
        if action == 'context':
            p.add_argument('--timeout', type=duration, default=120)
            p.add_argument('--max-pages', type=positive, default=100)
            p.add_argument('--page-size', type=positive, choices=range(1, 101), default=100, metavar='1..100')
            p.add_argument('--max-entries', type=positive, default=100000)
            p.add_argument('--require-patches', action='store_true', help='Refuse context if any file has no text patch')
    p = command(pr, "create", "Create PR once from an existing pushed branch; --dry-run prints offline details without HTTP or push")
    p.add_argument('-w', '--web', action='store_true', help='Open native creation form; branches/text selected in browser; prefill flags currently refuse; dry-run never launches browser')
    p.add_argument('--recover', metavar='FILE', help='Restore private gv JSON input saved after failed create; attempted writes require reconciliation, never replay')
    p.add_argument("-t", "--title", help="Required unless --recover/--editor/--fill/--fill-first/--fill-verbose; overrides restored/filled title; editor seed"); body(p)
    p.add_argument('-T', '--template', metavar='FILE_OR_NAME', help='Local UTF-8 file or exact repository template filename; offline dry-run leaves catalog names unresolved; conflicts with body/body-file')
    fill_mode = p.add_mutually_exclusive_group()
    fill_mode.add_argument('-f', '--fill', action='store_true', help='Use local commit title/body defaults; explicit title/body override; no fetch or push')
    fill_mode.add_argument('--fill-first', action='store_true', help='Use first local compared commit subject/body; explicit title/body override; no fetch or push')
    fill_mode.add_argument('--fill-verbose', action='store_true', help='Use local commit subjects and bodies for description; explicit title/body override; no fetch or push')
    p.add_argument("-e", "--editor", action="store_true", help="Skip prompts; edit title on first line and body on remaining lines; dry-run never launches editor")
    p.add_argument('--attach', action='append', metavar='FILE[#ALT]', help='Local --dry-run attachment plan only; live PR upload/linking unimplemented; repeat up to 50')
    p.add_argument("--timeout", type=duration, default=60, help="Shared input/API creation budget in seconds")
    p.add_argument("-H", "--head", help="Exact existing branch or USER:BRANCH; bounded fork resolution for cross-owner heads; no push/fork; defaults to current branch; cross-owner live mapping not yet verified")
    p.add_argument("-B", "--base", help="Target branch; defaults to current branch gh-merge-base Git config, then repository default")
    p.add_argument("-d", "--draft", action="store_true", help="Create a draft; add GitVerse Draft: title prefix unless already present")
    p.add_argument('--no-maintainer-edit', action='store_true', help='Disable maintainer changes to the author branch; verify maintainer_can_modify:false on creation and readback')
    p.add_argument('-a', '--assignee', action='append', help='Assignee login(s), comma-separated or repeated; @me supported')
    p.add_argument('-l', '--label', action='append', help='Existing label names, case-insensitive; comma-separated/repeated, up to 100; never creates labels')
    p.add_argument('-m', '--milestone', metavar='NAME', help='Milestone by literal name: offline --dry-run plan only; live name-to-ID contract unimplemented')
    p.add_argument('-p', '--project', action='append', metavar='TITLE', help='Project titles, repeated or CSV: offline --dry-run plan only; live PR membership contract unimplemented')
    p.add_argument('-r', '--reviewer', action='append', help='Reviewer login(s), ORG/TEAM-SLUG or team:ID; repeated/CSV; separate web cookies required')
    p = command(pr, "edit", "Edit title, description or base branch"); number(p); body(p)
    p.add_argument("-t", "--title"); p.add_argument("-B", "--base")
    for kind in ('assignee', 'label', 'reviewer'):
        p.add_argument('--add-' + kind, action='append', help='Add name(s), repeated or comma-separated' + ('; reviewers require web cookies; team:ID supported' if kind == 'reviewer' else ''))
        p.add_argument('--remove-' + kind, action='append', help='Remove name(s), repeated or comma-separated')
    p = command(pr, "review", "Submit a review, optionally with inline comments"); number(p); body(p)
    event = p.add_mutually_exclusive_group(required=True)
    event.add_argument("--approve", action="store_true")
    event.add_argument("--request-changes", action="store_true")
    event.add_argument("--comment", action="store_true")
    p.add_argument("--commit", help="Reviewed head SHA; defaults to current PR head")
    p.add_argument("--comments-file", help="JSON array: [{path, body, new_position|old_position}]")
    p.add_argument("--review-id", type=positive, help="Submit an existing PENDING review, without creating a new one")
    p = command(pr, 'comment-edit', 'Edit one own discussion comment by explicit ID')
    number(p); body(p)
    p.add_argument('--comment-id', type=positive, required=True)
    p.add_argument('--timeout', type=duration, default=60)
    p = command(pr, "comment", "Verified discussion creation; also inline comment or reply"); number(p); body(p)
    p.add_argument('-e', '--editor', action='store_true', help='Skip prompts; open GV_EDITOR/VISUAL/EDITOR (default vi) for discussion text; dry-run never launches editor')
    p.add_argument('-w', '--web', action='store_true', help='Open PR discussion in browser to write; --edit-last opens newest own comment; CLI sends no comment write')
    p.add_argument('--delete-last', action='store_true', help='Delete newest own public discussion comment; GitVerse may cascade thread replies')
    p.add_argument('--yes', action='store_true', help='Skip delete confirmation with --delete-last; required for noninteractive writes (not needed for --dry-run)')
    p.add_argument('--edit-last', action='store_true', help='Edit newest own public discussion by creation time then ID; absence refuses unless --create-if-none; equal text is no-op')
    p.add_argument('--create-if-none', action='store_true', help='Only with --edit-last: create when no own discussion comment exists')
    p.add_argument('--max-entries', type=positive, default=100000, help='Maximum discussion catalog entries for edit selection')
    p.add_argument('--attach', action='append', metavar='FILE[#ALT]', help='Experimental image/video upload to new discussion; live upload unresolved (422); repeat up to 50; offline dry-run')
    p.add_argument("--timeout", type=duration, default=60, help="Discussion input/write/readback budget in seconds")
    p.add_argument("--path"); p.add_argument("--line", type=positive)
    p.add_argument("--side", choices=["LEFT", "RIGHT"], default="RIGHT")
    p.add_argument("--commit"); p.add_argument("--reply-to", type=positive)
    p = command(pr, "resolve", "Resolve or reopen a website discussion thread")
    p.add_argument('targets', nargs='*', metavar='TARGET', help='[PR selector] COMMENT_ID; without selector use the current Git branch')
    p.add_argument("--unresolve", action="store_true")
    p = command(pr, "merge", "Merge using website API with an expected head SHA"); number(p)
    p.add_argument("--match-head-commit", required=True)
    p.add_argument("--method", choices=["merge", "squash", "rebase"], default="merge")
    p.add_argument("--title", required=True); body(p)

    api = group("api", "Arbitrary API calls and offline schema discovery")
    p = command(api, "request", "Call relative API endpoint")
    p.add_argument("endpoint"); p.add_argument("-X", "--method", default="GET", choices=["GET", "POST", "PATCH", "PUT", "DELETE", "HEAD"])
    p.add_argument("-f", "--field", action="append", help="String body field KEY=VALUE")
    p.add_argument("-q", "--query", action="append", help="Query parameter KEY=VALUE")
    p.add_argument("--input", help="JSON request body file, or -")
    p.add_argument("--raw", action="store_true")
    p = command(api, "endpoints", "List documented public endpoints or website candidates offline"); p.add_argument("--search", default="")
    p = command(api, "schema", "Inspect request/response schemas without network access")
    p.add_argument("path"); p.add_argument("-X", "--method", default="GET")

    auth = group("auth", "Token authentication")
    command(auth, "status", "Report credential source and verify current user")
    p = command(auth, "login", "Read PAT from stdin, validate and save with mode 0600")
    p.add_argument("--with-token", action="store_true", required=True)
    command(auth, "logout", "Remove the locally saved PAT")
    repo = group("repo", "Repository details")
    command(repo, "view", "View repository metadata")
    p = command(repo, 'set-default', 'Choose the default GitVerse repository for this Git clone')
    p.add_argument('default_repository', nargs='?', help='OWNER/REPO matching an existing GitVerse remote')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('-v', '--view', action='store_true')
    mode.add_argument('-u', '--unset', action='store_true')
    p.add_argument('--timeout', type=duration, default=60)
    p = command(repo, 'delete', 'Delete an explicit repository once, then verify absence')
    p.add_argument('--yes', action='store_true', help='Explicitly confirm irreversible repository deletion')
    p.add_argument('--match-id', type=positive, help='Require this immutable repository ID before deletion')
    p.add_argument('--timeout', type=duration, default=60)
    for action in ('archive', 'unarchive', 'rename'):
        p = command(repo, action, 'Repository ' + action + ' through public PAT API with readback')
        p.add_argument('--timeout', type=duration, default=60)
        if action == 'rename': p.add_argument('new_name', help='New repository name in the same owner namespace')
    p = command(repo, 'edit', 'Update selected repository settings with preflight and verified readback')
    p.add_argument('-d', '--description')
    p.add_argument('--homepage', help='HTTP(S) website URL, or empty string to clear')
    p.add_argument('--default-branch', help='Exact existing branch name, not a tag or commit')
    for option, dest in (('merge-commit', 'allow_merge_commit'), ('squash-merge', 'allow_squash_merge'), ('rebase-merge', 'allow_rebase_merge'), ('template', 'is_template')):
        values = p.add_mutually_exclusive_group()
        values.add_argument('--enable-' + option, dest=dest, action='store_true', default=None)
        values.add_argument('--disable-' + option, dest=dest, action='store_false')
    p.add_argument('--timeout', type=duration, default=60)
    p.add_argument('--max-pages', type=positive, default=100)
    p = command(repo, "list", "List repositories for current user"); listing(p)
    for action in ('contents', 'read-file', 'read-dir'):
        p = command(repo, action, 'Read pinned repository content through public API')
        p.add_argument('path', nargs=None if action == 'read-file' else '?', default='' if action != 'read-file' else argparse.SUPPRESS)
        p.add_argument('--ref', help='Branch, tag or full commit SHA; defaults to repository default branch')
        p.add_argument('--timeout', type=duration, default=60, help='Overall API/output budget in seconds')
        p.add_argument('--max-pages', type=positive, default=100)
        p.add_argument('--page-size', type=positive, default=100, help='Tree page size, maximum 100')
        p.add_argument('--max-entries', type=positive, default=100000, help='Maximum full-tree entries scanned')
        if action in ('contents', 'read-file'):
            p.add_argument('--max-bytes', type=positive, default=10485760, help='Maximum decoded file bytes (default 10 MiB)')
        if action in ('contents', 'read-dir'):
            p.add_argument('--recursive', action='store_true', help='Include all descendants for directory output')
        if action == 'read-file':
            mode = p.add_mutually_exclusive_group()
            mode.add_argument('--raw', action='store_true', help='Write exact bytes to stdout, without an added newline')
            mode.add_argument('-o', '--output', help='Save exact bytes to a new local file; never overwrite')
    p = command(repo, 'clone', 'Clone GitVerse over HTTPS with process-only PAT credentials')
    p.add_argument('source', help='OWNER/REPO or canonical GitVerse HTTPS URL')
    p.add_argument('directory', nargs='?', help='New directory; defaults to repository name')
    p.add_argument('-b', '--branch')
    p.add_argument('--depth', type=positive)
    p.add_argument('--single-branch', action='store_true')
    p.add_argument('--timeout', type=duration, default=300)
    p = command(repo, 'create', 'Create a personal or organization repository once')
    p.add_argument('name', help='NAME or OWNER/NAME; owner must be current user or an organization')
    visibility = p.add_mutually_exclusive_group(required=True)
    visibility.add_argument('--private', dest='private', action='store_true')
    visibility.add_argument('--public', dest='private', action='store_false')
    p.add_argument('-d', '--description', default='')
    p.add_argument('--add-readme', action='store_true')
    p.add_argument('--gitignore', action='append')
    p.add_argument('--clone', action='store_true')
    p.add_argument('--dir', help='New clone directory; requires --clone')
    p.add_argument('--timeout', type=duration, default=300)
    p = command(repo, 'fork', 'Fork explicitly, verify parent and optionally clone; no remote rewrites')
    p.add_argument('source', nargs='?', help='OWNER/REPO or HTTPS URL; defaults to current GitVerse repo/-R')
    p.add_argument('--fork-name', help='Name for the target repository')
    p.add_argument('--org', help='Target organization; otherwise current user')
    p.add_argument('-d', '--description')
    p.add_argument('--clone', action='store_true')
    p.add_argument('--dir', help='New clone directory; requires --clone')
    p.add_argument('--timeout', type=duration, default=300)
    run = group("run", "CI runs")
    p = command(run, "list", "Filter a complete bounded CI run catalog"); listing(p)
    p.add_argument('-b', '--branch', help='Exact branch name, or refs/heads/NAME')
    p.add_argument('-e', '--event', help='Exact native event, e.g. workflow_dispatch')
    p.add_argument('-s', '--status', help='Exact native status, or completed for known terminal statuses')
    p.add_argument('-u', '--user', help='Actor login or @me')
    p.add_argument('-c', '--commit', help='Full 40/64 hexadecimal commit SHA')
    p.add_argument('-w', '--workflow', help='Workflow filename or exact unique display name; numeric IDs unsupported')
    p.add_argument('--max-pages', type=positive, default=100)
    p.add_argument('--timeout', type=duration, default=60)
    p = command(run, 'download', 'Download artifacts for an explicit run into a new directory')
    p.add_argument('run_id', type=positive)
    p.add_argument('-n', '--name', action='append', help='Exact artifact name; repeatable; every name must match')
    p.add_argument('-D', '--dir', help='New output directory; default artifacts-RUN_ID; existing paths refused')
    p.add_argument('--archive', action='store_true', help='Keep ZIP files instead of extracting')
    p.add_argument('--max-pages', type=positive, default=100, help='Maximum repository artifact pages')
    p.add_argument('--timeout', type=duration, default=300, help='Overall metadata/download/extraction budget in seconds')
    p.add_argument('--max-bytes', type=positive, default=268435456, help='Total compressed byte limit (default 256 MiB)')
    p.add_argument('--max-extracted-bytes', type=positive, default=1073741824, help='Total uncompressed byte limit (default 1 GiB)')
    p.add_argument('--max-files', type=positive, default=10000, help='Total ZIP entry limit (default 10000)')
    p = command(run, 'watch', 'Wait for a CI run and all visible jobs; emit one final JSON result')
    p.add_argument('run_id', type=positive)
    p.add_argument('-i', '--interval', type=duration, default=10, help='Polling interval in seconds (default 10)')
    p.add_argument('--timeout', type=duration, default=300, help='Overall request/watch budget in seconds (default 300)')
    p.add_argument('--max-pages', type=positive, default=20, help='Maximum job pages per poll; incomplete scans fail')
    p.add_argument('--exit-status', action='store_true', help='Exit 1 on workflow or job failure/cancellation; otherwise terminal outcomes exit 0')
    for a in ("view", "jobs", "cancel", "rerun"):
        p = command(run, a, a + " CI run"); p.add_argument("run_id", type=positive)
        if a == 'view':
            logs = p.add_mutually_exclusive_group()
            logs.add_argument('--log', action='store_true', help='Return complete job logs as JSON; run must be finished')
            logs.add_argument('--log-failed', action='store_true', help='Return logs of failed steps only; run must be finished')
            p.add_argument('--job', type=positive, help='Restrict log output to this job ID within the run')
            p.add_argument('--max-pages', type=positive, default=20, help='Maximum job pages for log collection')
            p.add_argument('--timeout', type=duration, default=120, help='Overall log request budget in seconds')
    workflow = group('workflow', 'Discover workflows and explicitly dispatch CI')
    p = command(workflow, 'list', 'List active workflows; --all includes inactive ones')
    listing(p)
    p.add_argument('-a', '--all', action='store_true')
    p.add_argument('--max-pages', type=positive, default=20)
    p.add_argument('--timeout', type=duration, default=60)
    for action in ('view', 'run'):
        p = command(workflow, action, 'Inspect workflow metadata/inputs' if action == 'view' else 'Dispatch workflow once; no automatic write retry')
        p.add_argument('workflow', help='Workflow filename (.yml/.yaml) or exact unique display name; not numeric ID')
        target = p.add_mutually_exclusive_group()
        target.add_argument('-r', '--ref', help='Branch name; default is repository default branch')
        target.add_argument('--tag', help='Tag name (use explicitly instead of --ref)')
        p.add_argument('--max-pages', type=positive, default=20)
        p.add_argument('--timeout', type=duration, default=60)
        if action == 'view':
            p.add_argument('--inputs', action='store_true', help='Also inspect workflow_dispatch inputs for --ref/--tag or default branch')
        if action == 'run':
            inputs = p.add_mutually_exclusive_group()
            inputs.add_argument('-f', '--raw-field', '--field', dest='input_fields', action='append', help='String input KEY=VALUE; repeatable; no @file expansion')
            inputs.add_argument('--input', help='UTF-8 JSON object of string inputs, or - for stdin')
    traffic = group("traffic", "Analyze captured website traffic offline")
    p = command(traffic, "analyze", "Produce inventory without payloads or credential values")
    p.add_argument("files", nargs="+")
    p = command(groups, "agent-info", "Machine-readable capabilities and examples")
    return root


def reviewed_sha(args, client, path):
    if args.commit:
        return args.commit
    if client.dry_run:
        raise GVError("Pass --commit SHA for a deterministic review dry-run", "usage_error")
    pr = client.request("GET", path)
    sha = pr.get("head", {}).get("sha")
    if not sha:
        raise GVError("PR response has no head.sha; pass --commit explicitly", "unexpected_response")
    return sha


def run_pr(args, client, repo):
    if args.action == 'comment-edit':
        require_public(client, 'pr comment-edit')
        from .comment_edit import edit_comment
        return edit_comment(client, repo, args)
    a = args.action
    path = f"repos/{repo}/pulls"
    query = {}
    if a == "list":
        from .pulls import list_pulls
        return list_pulls(args, client, repo)
    if a == "create":
        require_public(client, "pr create")
        if args.web:
            from .pr_create_web import web_create
            return web_create(client, repo, args)
        from .pr_create import create_pull
        return create_pull(client, repo, args)
    path += "/" + str(args.number)
    if a == 'close':
        from .pr_close import close_pull
        return close_pull(client, repo, args)
    if a == "view":
        return client.request("GET", path)
    if a in ("files", "commits", "reviews", "comments", "threads"):
        if a == "threads":
            if client.backend != "web":
                raise GVError("Use --backend web with GITVERSE_COOKIE_FILE for rich threads", "backend_unsupported")
            return client.request("GET", path + "/comments")
        require_public(client, "pr " + a)
        target = f"repos/{repo}/issues/{args.number}/comments" if a == "comments" else path + "/" + a
        if a == "comments":
            return client.request("GET", target)  # Spec has no pagination parameters here.
        return client.pages(target, {"page": args.page}, args.paginate, min(args.limit, 100), "limit" if a == "reviews" else "per_page")
    if a == "context":
        require_public(client, "pr context")
        from .pr_context import collect_context
        return collect_context(client, repo, args)
    if a == "diff":
        require_public(client, "pr diff")
        files = client.pages(path + "/files", paginate=True, page_size=100)
        if client.dry_run:
            return files
        missing = [f.get("filename") for f in files if not f.get("patch")]
        if args.patch:
            if missing:
                raise GVError("Some files have no patch (binary, empty or truncated); use `pr files`", "incomplete_diff", details={"files": missing})
            return "\n".join(unified_patch(f) for f in files) + "\n"
        return {"files": files, "missing_patches": missing, "complete": not missing}
    if a in ("edit", "reopen", "update-branch"):
        require_public(client, "pr " + a)
        if a == "update-branch":
            return client.request("PUT", path + "/update-branch")
        payload = {"state": "closed" if a == "close" else "open"} if a != "edit" else {}
        if a == "edit":
            for key in ("title", "base"):
                if getattr(args, key) is not None:
                    payload[key] = getattr(args, key)
            text = body_text(args)
            if text is not None:
                payload["body"] = text
            if any(getattr(args, direction + '_' + kind) for direction in ('add', 'remove') for kind in ('assignee', 'label', 'reviewer')):
                from .metadata import edit_with_metadata
                return edit_with_metadata(args, client, repo, args.number, payload)
            if not payload:
                raise GVError("No changes specified", "usage_error")
        return client.request("PATCH", path, body=payload)
    if a == "review":
        require_public(client, "pr review")
        event = "APPROVED" if args.approve else "REQUEST_CHANGES" if args.request_changes else "COMMENT"
        text = body_text(args)
        if args.review_id:
            if args.comments_file or args.commit:
                raise GVError("Existing review already has comments and a commit; omit --comments-file/--commit", "usage_error")
            submit_body = {"event": event}
            if text is not None:
                submit_body["body"] = text
            return submit_review(client, path, args.review_id, submit_body)
        comments = read_json(args.comments_file) if args.comments_file else []
        validate_comments(comments)
        text = text or ""
        if not args.approve and not text.strip() and not comments:
            raise GVError("Review requires a body or inline comments", "usage_error")
        if comments and not text.strip():
            text = f"Inline review comments: {len(comments)}."
        payload = {"event": event, "body": text, "commit_id": reviewed_sha(args, client, path)}
        # Live GitVerse currently fails with HTTP 500 on reviews containing
        # comments, leaving a PENDING review behind. The standalone inline
        # endpoint works. Publish those first, then the summary review.
        inline_results = []
        try:
            for index, comment in enumerate(comments):
                old = comment.get("old_position", 0) > 0
                inline_body = {"path": comment["path"], "body": comment["body"], "line": comment["old_position" if old else "new_position"], "side": "LEFT" if old else "RIGHT", "commit_id": payload["commit_id"]}
                inline = client.request("POST", path + "/comments", body=inline_body)
                if not client.dry_run and (not isinstance(inline, dict) or type(inline.get("id")) is not int):
                    raise GVError("Inline comment outcome unconfirmed; inspect comments before retrying", "unexpected_response")
                inline_results.append(inline)
        except GVError as e:
            e.details = {"stage": "inline_comment", "failed_comment_index": index, "published_inline_comment_ids": [c["id"] for c in inline_results], "recovery": "Inspect published comments before retrying; the failed request may also have applied. Summary review was not created."}
            raise
        try:
            result = client.request("POST", path + "/reviews", body=payload)
            if client.dry_run:
                result["inline_requests_before_review"] = inline_results
                result["then_if_pending"] = client.request("POST", path + "/reviews/{review_id}/events", body={"event": event, "body": text})
                return result
            if isinstance(result, dict) and str(result.get("state", "")).upper() == "PENDING":
                review_id = result.get("id")
                if type(review_id) is not int or review_id < 1:
                    raise GVError("Review created as PENDING but no usable ID returned; inspect pr reviews before retrying", "unexpected_response")
                result = submit_review(client, path, review_id, {"event": event, "body": text})
            else:
                result = verify_review(result, event)
        except GVError as e:
            e.details = {"stage": "create_review", "recovery": "Inspect pr reviews before retrying", **(e.details or {}), "published_inline_comment_ids": [c["id"] for c in inline_results]}
            raise
        if inline_results:
            result["inline_comment_ids"] = [c["id"] for c in inline_results]
            result["inline_comments_published_separately"] = True
        return result
    if a == "comment":
        require_public(client, "pr comment")
        if args.attach and (args.reply_to or args.path is not None or args.line is not None or args.commit):
            raise GVError('--attach applies to discussion creation, not inline comments or replies', 'usage_error')
        if args.delete_last:
            from .comment_delete import delete_last
            return delete_last(client, repo, args)
        if args.edit_last or args.create_if_none or (not args.reply_to and args.path is None and args.line is None):
            from .pr_comment import discussion_comment
            return discussion_comment(client, repo, args)
        text = body_text(args, required=True)
        if args.reply_to:
            if args.path or args.line:
                raise GVError("--reply-to cannot be combined with --path/--line", "usage_error")
            return client.request("POST", path + "/comments", body={"body": text, "in_reply_to": args.reply_to})
        if args.path is not None or args.line is not None:
            if not args.path or args.line is None:
                raise GVError("Inline comment requires both --path and --line", "usage_error")
            return client.request("POST", path + "/comments", body={"body": text, "path": args.path, "line": args.line, "side": args.side, "commit_id": reviewed_sha(args, client, path)})
        return client.request("POST", f"repos/{repo}/issues/{args.number}/comments", body={"body": text})
    if a == "resolve":
        if client.backend != "web":
            raise GVError("Thread resolution requires --backend web and a website session cookie file", "backend_unsupported")
        return client.request("PATCH", path + f"/comments/{args.comment_id}/resolve", {"action": "unresolve" if args.unresolve else "resolve"})
    if a == "merge":
        # Public v1 currently documents GET merge status, but no merge mutation.
        if client.backend != "web":
            raise GVError("Merge mutation uses the observed website API; pass --backend web", "backend_unsupported")
        return client.request("POST", path + "/merge", body={"action": args.method, "title": args.title, "message": body_text(args) or "", "headCommitId": args.match_head_commit, "mergeWhenChecksSucceed": False, "deleteBranch": False})
    raise GVError("Unknown PR command", "usage_error")


def unified_patch(file):
    def name(value):
        return json.dumps(value, ensure_ascii=False) if re.search(r'[\s"\\]', value) else value
    old = "a/" + (file.get("previous_filename") or file["filename"])
    new = "b/" + file["filename"]
    before = "/dev/null" if file.get("status") == "added" else old
    after = "/dev/null" if file.get("status") == "removed" else new
    mode = "new file mode 100644\n" if file.get("status") == "added" else "deleted file mode 100644\n" if file.get("status") == "removed" else ""
    patch = file['patch'].rstrip('\n')
    return f"diff --git {name(old)} {name(new)}\n{mode}--- {name(before)}\n+++ {name(after)}\n{patch}"


def verify_review(result, event):
    expected = {"APPROVED": {"APPROVED"}, "REQUEST_CHANGES": {"REQUEST_CHANGES", "CHANGES_REQUESTED"}, "COMMENT": {"COMMENT", "COMMENTED"}}[event]
    if not isinstance(result, dict) or str(result.get("state", "")).upper() not in expected:
        raise GVError("Server did not confirm the expected published review state; inspect pr reviews before retrying", "review_state_unconfirmed", details={"review_id": result.get("id") if isinstance(result, dict) else None})
    return result


def submit_review(client, path, review_id, body):
    try:
        result = client.request("POST", path + f"/reviews/{review_id}/events", body=body)
        return result if client.dry_run else verify_review(result, body["event"])
    except GVError as e:
        e.details = {"review_id": review_id, "stage": "submit", "recovery": "Inspect pr reviews before retrying pr review with --review-id; do not create another review"}
        raise


def validate_comments(comments):
    if not isinstance(comments, list):
        raise GVError("Comments file must contain a JSON array", "usage_error")
    for c in comments:
        if not isinstance(c, dict) or set(c) - {"path", "body", "new_position", "old_position"}:
            raise GVError("Inline review fields: path, body, new_position, old_position", "usage_error")
        if not isinstance(c.get("path"), str) or not c["path"] or not isinstance(c.get("body"), str) or not c["body"].strip():
            raise GVError("Each inline comment needs path and non-empty body", "usage_error")
        positions = [c[k] for k in ("new_position", "old_position") if k in c and c[k] != 0]
        if len(positions) != 1 or type(positions[0]) is not int or positions[0] < 1:
            raise GVError("Use one positive new_position or old_position (file line number)", "usage_error")


def execute(args):
    if args.group == 'pr' and args.action == 'comment':
        from .pr_comment import validate_mode
        validate_mode(args)
    if args.group == 'pr' and args.action == 'checks':
        from .checks import validate_json
        validate_json(args)
    if args.group == 'repo' and args.action == 'set-default':
        from .repo_default import set_default
        return set_default(args)
    if args.group in ('config', 'alias'):
        from .settings import local_command
        return local_command(args)
    if args.group == "agent-info":
        result = {"name": "gv", "version": __version__, "output": "JSON stdout, JSON errors stderr; raw only on explicit --patch/--raw", "auth": {"public": "GITVERSE_TOKEN or GV_TOKEN", "web": "GITVERSE_COOKIE_FILE (Netscape cookie jar)"}, "primary_flow": ["gv pr create -R OWNER/REPO --head feature --base main --title 'Change' --body-file description.md", "gv pr context 42 -R OWNER/REPO", "gv pr review 42 -R OWNER/REPO --request-changes --commit REVIEWED_SHA --body-file review.md --comments-file comments.json"], "discovery": ["gv --help", "gv pr --help", "gv api endpoints --search pulls", "gv api schema '/repos/{owner}/{repo}/pulls/{pull_number}/reviews' -X POST"], "writes": "Checkout changes the local Git working tree. Explicit create/edit/ready/comment/review/close/reopen/update-branch/resolve/merge/cancel/rerun/API mutations change remote state. --dry-run emits requests without network.", "limits": "Inline remarks publish separately before the summary review because the server's atomic batch endpoint returns HTTP 500. Partial failures include published comment IDs. Self-approval/request changes need another account. Internal web routes require a separate cookie session and have not been live-validated. pr checks covers base-repo CI only; external statuses and required branch checks are unknown. Only --ci-only permits exit 0. pr ready/--undo changes the Draft: title prefix using public PAT, verifies draft/title, and refuses closed PRs. Title concurrency checks are best effort; no atomic condition is documented. PR selectors accept number/URL/unique open head/current branch. Status covers this repo, direct review requests, optional CI; team membership and approval requirements unknown. Branch dry-run emits selection plans only. Create/edit assignees and labels use public PAT and verify GET outcomes. Reviewer changes need cookie-only web API with authenticated candidate preflight, have contract tests only, and are not atomic with PR creation/other metadata. Errors retain pull_number/stage/completed_steps; read state before retry. Metadata dry-run keeps identities unresolved."}
        result['local_settings'] = {'commands': ['config list/get/set/unset/path', 'alias list/set/import/delete'], 'keys': ['default_repo', 'http_timeout'], 'path_env': 'GV_SETTINGS', 'auth_separate': True, 'default_repo_scope': 'Fallback outside Git only; -R/GV_REPO/GitVerse remote preserve priority', 'http_timeout_env': 'GV_HTTP_TIMEOUT', 'aliases': {'argv_only': True, 'shell': False, 'standalone_placeholders': ['$1..$99', '$@'], 'max_depth': 16}}
        result['discovery'].extend(['gv config list --effective', 'gv alias list'])
        result['pr_create_web'] = {'flags': ['--web', '-w'], 'status': 'partial', 'behavior': 'Open native /OWNER/REPO/compare with verified same-repository baseBranch/headBranch URL prefill. CLI never submits a PR. Bare handoff outside Git still permits manual branch selection.', 'branch_selection': 'Explicit --head/--base; otherwise infer current branch only with one matching GitVerse remote, configured gh-merge-base then online repository default. Verify exact branch names/SHAs twice, repository id/privacy/archive/default twice, and local inferred selection before launch. No push/fetch.', 'exclusions': 'Reject editor/draft/reviewer/no-maintainer-edit/attach. Text/fill/template/metadata/recover and qualified cross-repository heads refuse feature_unavailable; no guessed URL parameters or discarded inputs.', 'bounds': 'Shared timeout60 and complete HTTP32MiB for repository/branch GETs and launcher; local Git4MiB. No pagination or API mutation/retry; no atomic snapshot.', 'browser': 'Credential-free GV_BROWSER argv, no shell; separate browser session. branches_prefilled and branches_verified describe URL/API checks; branches_selected/form_verified remain false. Launcher acknowledgment is not browser verification.', 'dry_run': 'Offline local branch/config reads and encoded URL. Missing default base remains unresolved; no auth/HTTP/browser/push/write.', 'remaining': 'Native text/fill/template/metadata/recover/cross-repository prefill contract and implementation; positive installed real-browser worker evidence and independent verifier reproduction. Only --web scope.'}
        result['pr_create_template'] = {'flags': ['--template', '-T'], 'behavior': 'Explicit local UTF-8 regular file as starting body text; preserve bytes as text, including blank/CRLF/front matter. Title remains explicit or filled/editor input. Editor may replace seed. No prompts or implicit discovery.', 'combinations': 'Reject body/body-file, even empty, before reading input. Like gh, direct fill submission ignores template; with editor it replaces filled body. Explicit template overrides restored body; recovery stores resolved body, never template path.', 'bounds': '1MiB UTF-8 without NUL; local symlinks/FIFO/directories/stdin refused. Catalog tree <=100 pages of100,100000 entries; selected Git blob size/hash verified at pinned commit. Shared timeout60; complete HTTP32MiB. Repository ID rechecked before POST.', 'dry_run': 'No auth/network/editor/push. Local files resolved offline; catalog names/body unresolved, final_request_known:false, request body omitted. Reports source/name/resolved/applied/body_known. Editor seed/final text remains unknown for catalog names; metadata stays unresolved.', 'verification': 'Existing one POST201 plus independent exact text/author/repo/ref/SHA readback; no no-op/idempotency/CAS/retry; preserve recovery IDs.', 'remaining': 'gv noninteractive adaptation: bare names without an existing local path select exact .md filenames from the default-branch repository catalog via verified public Git tree/blob reads. Precedence .github/PULL_REQUEST_TEMPLATE, PULL_REQUEST_TEMPLATE, docs/PULL_REQUEST_TEMPLATE (directory case-insensitive). First nonempty catalog wins. No implicit template application, prompts or browser handoff. Existing pushed branches required; parent/sibling parity separate.'}
        result['pr_create_no_maintainer_edit'] = {'flags': ['--no-maintainer-edit'], 'behavior': 'Send public maintainer_can_modify:false to disable maintainer changes to the author branch; require literal false in POST201 and independent final GET. Omission preserves server default.', 'dry_run': 'Offline exact false, including metadata/editor plans; no auth/network/push or permission prediction.', 'bounds': 'Shared timeout60 and complete HTTP32MiB; no additional pagination.', 'recovery': 'One creation per invocation, never a no-op or automatic retry; no CAS. Errors retain known PR IDs/scope/stage/mutation outcome; reconcile before another creation.', 'remaining': 'Existing published branches required; does not revoke existing repository write permissions. Parent/sibling parity separate; independent positive installed verification required.'}
        result['pr_create_title'] = {'flags': ['--title', '-t'], 'behavior': 'Literal explicit PR title; overrides restored and fill/fill-first/fill-verbose titles independently of body. With --editor it is the editable first-line seed. No prompt.', 'adaptation': 'Nonblank single-line UTF-8 <=1MiB, no NUL/CR/LF; preserve whitespace and Unicode. --draft adds native Draft: prefix once, within the same byte bound. Already draft-prefixed text retains spelling; server draft/state must match requested mode.', 'verification': 'One public PAT POST201 and independent GET require exact title plus author/repository/refs/SHAs/state. No no-op/idempotency/CAS/automatic retry; errors retain known PR IDs and scope for reconciliation.', 'dry_run': 'Offline exact title or editor seed with unknown final title; no auth/network/editor/push. Metadata remains unresolved; explicit/configured base required.', 'bounds': 'Shared timeout60, complete HTTP responses32MiB. Title itself adds no pagination; metadata retains its existing catalog bounds.', 'remaining': 'Existing published branches required. JSON output and explicit noninteractive input are gv adaptations; parent and sibling flag parity remain separate.'}
        result['pr_create_recover'] = {'flags': ['--recover'], 'behavior': 'Restore input from private version1 gv JSON saved as recover_file after a failed create; no prompts. Restore title/body/draft/metadata/head/base; explicit text and metadata override, scope changes refuse. Editor uses restored text as seed; fill only supplies missing values.', 'safety': 'Repository and exact public API bound; private owned singly linked regular file, no symlinks/stdin. Exclusive nonblocking lock and persistent guard before POST prevent replay/concurrent use. Attempted writes refuse live recovery; reconcile known IDs/head/base; offline inspection remains available.', 'bounds': 'Recovery JSON16MiB, title/body1MiB, shared timeout60; existing HTTP32MiB and metadata pagination bounds apply.', 'dry_run': 'Read-only local validation/restoration with replay_allowed; no network/auth/editor/push or recovery file writes. Remote permission/metadata remain unverified.', 'remaining': 'gv noninteractive adaptation: gh currently restricts recovery to interactive sessions. gv files are not gh internal state files. No automatic mutation retry/rollback/CAS; recovery of an attempted write is inspection only, not resumption of partial metadata steps. Parent/sibling parity separate.'}
        result['directory'] = {'commands': ['org view/repos/member', 'team list/view/members/repos'], 'backend': 'public', 'team_selector': 'ORG/SLUG', 'scope': 'Visible to authenticated user; no org list/member catalog or permission writes', 'verification': 'Positive team/member responses contract-tested; live access depends on PAT scopes and organization permissions'}
        result['discovery'].extend(['gv org --help', 'gv team --help'])
        result['pr_checks'] = {'scope': 'Core status inspection: pr checks NUMBER -R OWNER/REPO --ci-only; exact-head base-repository visible CI; public PAT Repository Read; non-atomic; cancelled/failed CI returns1', 'bounds': '1 MiB per complete response, empty-page exhaustion, max-pages20, max-entries100000 combined per poll, timeout300 after selector resolution', 'unresolved': ['external commit statuses', 'required checks', 'documented attempt ordering', 'same-SHA concurrent reruns', 'jq', 'template', 'web'], 'output': 'JSON envelope by default; --json FIELD,... exports selected check rows with checks_metadata on stderr, preserving safe exits; bare --json keeps envelope. Export rejects --watch/--fields; --required refuses; only --ci-only permits exit0'}
        result['repository_settings'] = {'command': 'gv repo edit', 'backend': 'public', 'fields': ['description', 'homepage', 'default_branch', 'merge_commit', 'squash_merge', 'rebase_merge', 'template'], 'verification': 'Preflight, equal-value noop, existing branch catalog, requested-field concurrency guard, one PATCH, separate GET preserving observed settings and identity/visibility; no atomic CAS', 'unsupported': ['rename', 'visibility', 'archive', 'delete_branch_on_merge']}
        result['run_listing'] = {'filters': ['branch', 'event', 'native status or completed', 'user or @me', 'full commit SHA', 'workflow filename or exact unique display name'], 'output': 'Always JSON envelope: workflow_runs, total_count/total_matches, page, page_size, has_more, filters, coverage; --paginate also returns envelope', 'scope': 'Complete bounded visible catalog before local filters/paging; workflow uses verified metadata and scoped route; no implicit latest run or CI pass', 'bounds': ['timeout', 'max-pages'], 'unsupported': ['created/date syntax', 'all/disabled-workflow semantics', 'numeric workflow IDs', 'SHA prefixes', 'gh jq/template/export schema']}
        result['repository_lifecycle'] = {'commands': ['repo archive', 'repo unarchive', 'repo rename NEW_NAME'], 'backend': 'public PAT', 'verification': 'Identity/archive preflight and recheck, equal-state noop, one PATCH and explicit destination GET; no redirects followed or atomic CAS', 'rename': 'Same owner only; require unarchive before rename; case-only name changes are noop; no local remote/settings updates; backend enforces destination availability'}
        result['repository_deletion'] = {'command': 'gv repo delete -R OWNER/REPO --yes [--match-id ID]', 'backend': 'public PAT', 'requirements': 'Explicit -R, --yes for writes, confirmed admin permission, identity/state preflight and recheck; dry-run requires explicit -R but no --yes', 'verification': 'One DELETE requiring empty HTTP204, separate GET404; no global name-availability claim, redirects, automatic retries, rollback, local cleanup or atomic CAS', 'missing_repository': 'Error, not an idempotent deletion success'}
        result['commit_search'] = {'command': 'gv search commits QUERY -R OWNER/REPO [--ref BRANCH_OR_TAG_OR_SHA]', 'scope': 'Literal commit messages in pinned commit plus all reachable ancestors; no global index', 'verification': 'Pinned commit GET identity/message/parents matches first list row; complete closed acyclic reachable parent graph before output; Git-object hashes/signatures not verified', 'bounds': ['timeout', 'max-pages', 'page-size <=50', 'max-entries', 'max-matches'], 'output': 'results sha/message/parents with commit_sha and coverage; no partial results on bounds or incomplete history'}
        result['clone_default'] = {'command': 'gv repo set-default OWNER/REPO | --view | --unset', 'storage': 'Local Git config gitverse.defaultRepository; shared by worktrees using that config; global/include config ignored', 'priority': '-R > GV_REPO > saved local choice matching existing GitVerse remote > unique GitVerse remote; settings default_repo only outside Git', 'auth': 'New selection verifies public API identity; view/unset/noop/dry-run do not need auth', 'limits': 'No -R/backend for this local command, no interactive or URL/remote-name selector, no Git remote rewrite or remote mutations, no atomic CAS; explicit --unset repairs stale/duplicate/malformed keys'}
        result['pr_create_fill_verbose'] = {'flag': '--fill-verbose', 'behavior': 'Local git log --cherry base...head non-merge commits. Single commit subject and literal body including final LF. Multiple commits: humanized head title; oldest-first - **subject** LF followed by each body with two spaces at every line start (including empty/final lines), two extra LFs between commits. Explicit title/body/file/stdin override independently; editor edits the resulting seed.', 'combinations': 'Mutually exclusive with --fill/-f and --fill-first. Existing draft normalization and metadata preflight/credential boundaries apply.', 'requirements': 'Complete local history, exactly one matching GitVerse remote, remote-tracking base and local head. No fetch/push/fork. Local refs/remotes rechecked and public branch SHAs must match local pins before POST and creation/readback.', 'bounds': 'Shared timeout60; Git output4MiB per read, commits1000, final title/body1MiB UTF-8 without NUL; HTTP complete32MiB. No pagination introduced, optional labels retain100-page bounds. Incomplete/ambiguous/malformed/changed inputs refuse.', 'dry_run': 'Offline local Git/input only, no HTTP/auth/editor/push. Explicit/configured base required; fill.mode:verbose and remote_verified:false. Metadata/editor unknowns remain explicit.', 'recovery': 'One public PAT POST201 plus independent exact-text/identity/ref/SHA GET. No no-op/idempotency/CAS/retry. Preserve scope/stage/mutation_attempted and known PR IDs; reconcile before another create.', 'remaining': 'Templates/browser/push/fork and parent parity remain separate.'}
        result['pr_create_fill_first'] = {'flag': '--fill-first', 'behavior': 'Use subject and literal body (including final LF) of the last commit in native newest-first git log --cherry base...head order. Merge and base-only commits excluded. Explicit title/body/file/stdin override independently; editor edits the resulting seed.', 'combinations': 'Mutually exclusive with --fill/-f and --fill-verbose. Draft and metadata retain their existing preflight and credential boundaries.', 'requirements': 'Complete local Git history, exactly one matching GitVerse remote, remote-tracking base and local head. No fetch/push/fork. Recheck local refs, public branch SHAs and creation/readback pins.', 'bounds': 'Shared timeout60, Git output4MiB per read, commits1000, final title/body1MiB, HTTP complete32MiB. No pagination introduced. Incomplete/malformed/ambiguous/changed history refuses.', 'dry_run': 'Offline local Git/input only; no HTTP/auth/editor/push. Explicit/configured base required; fill.mode:first and remote_verified:false. Metadata/editor unknowns remain explicit.', 'recovery': 'One POST201 and independent exact-text/identity/ref/SHA GET; no no-op/idempotency/CAS/retry. Preserve repository/head/base/stage/mutation_attempted and known PR IDs; reconcile before another create.', 'remaining': 'templates/browser/push/fork and parent parity remain separate.'}
        result['pr_create_fill'] = {'flags': ['--fill', '-f'], 'behavior': 'Local Git --cherry base...head non-merge commits. Single commit subject/body; multiple commits use branch name with hyphens/underscores replaced by spaces and oldest-first - **subject**\n bullets. Explicit title/body/file/stdin override defaults; editor edits the resulting seed.', 'requirements': 'Complete local Git history; exactly one matching GitVerse remote, remote-tracking base and local head. No fetch/push/fork. Ref pins rechecked locally and against public branch GETs before creation; POST/readback SHAs must match.', 'bounds': 'Shared timeout60, Git output4MiB, commits1000, final title/body1MiB; no pagination. Shallow/grafted/missing/ambiguous/changed history refuses without partial defaults.', 'dry_run': 'Offline local Git reads, no HTTP/auth/editor/push; explicit/configured base required; fill.remote_verified:false. Existing final_request_known metadata/editor rules apply.', 'recovery': 'Existing public PAT single POST201 and independent readback; no retry/idempotency/CAS/no-op promise. Errors retain known PR IDs; reconcile before another invocation.', 'remaining': 'templates and parent parity remain separate.'}
        result['pr_create_editor'] = {'flags': ['--editor', '-e'], 'behavior': 'Skip prompts; open one title/body draft. First line is title (LF or CRLF separator); remaining body preserved literally. Title-only draft permits empty body.', 'seeds': 'Optional --title and --body/--body-file/-F/- seed the draft; editor may replace both, including when both supplied. Commit defaults only with explicit --fill, --fill-first or --fill-verbose; no implicit template autofill.', 'editor': 'GV_EDITOR > VISUAL > EDITOR > vi; trusted shell-style argv without a shell, private Markdown file, TTY or null stdio, credential env removed; output isolated from JSON.', 'bounds': 'Shared timeout60 including editor; title/body each1MiB UTF-8 without NUL, nonblank single-line title. Combined draft <=2097154 bytes. Failed/invalid/timed-out editor submits nothing.', 'dry_run': 'No editor/draft/network/auth/push; editor.seed reports local seeds, title_known:false/body_known:false and final_request_known:false; request template omits unknown title/body. Explicit/configured base required.', 'verification': 'Public PAT existing single POST201 and independent exact text/identity/ref/SHA readback; draft normalization and metadata safety retained. No no-op/idempotency/CAS/retry; preserve known recovery IDs.', 'remaining': 'Existing remote branches required; sibling template/browser/attachment and parent parity separate.'}
        result['pr_create_dry_run'] = {'flag': '--dry-run', 'behavior': 'Print local creation details without HTTP, authentication loading, Git push or PR creation', 'output': 'Existing request/template keys plus dry_run, mutation_attempted:false, mutation_outcome:none, network_requests:0, git_push:false and validation flags', 'requirements': 'Explicit/configured base; explicit head or current Git branch; title, --editor, --fill, --fill-first or --fill-verbose; body/file/stdin validated locally', 'limits': 'No existence, permission, eligibility or PR ID prediction; metadata stays unresolved, final_request_known:false with metadata/attachments/editor. Attachments remain local plans only. timeout60; body/title1MiB. gh may push during dry-run; gv never pushes. Parent/sibling parity is separate.'}
        result['pr_create_attach'] = {'flag': '--attach', 'status': 'local dry-run plan only; live unimplemented; not verified parity', 'required_behavior': 'Upload up to 50 image/video files, rewrite body references or append; preserve successful uploads and create PR on partial upload failure with a nonzero result and recovery identity', 'research': 'Public issue-body attachment route explicitly excludes PRs. Retained web frontend uses a separate cookie-only PR uploader with multipart field file; creation/linking DTO and fresh positive path remain unverified.', 'limitations': 'Comment uploads are a different resource and do not implement PR creation attachments. No PAT/cookie forwarding or guessed upload/linking requests. --dry-run validates regular local files (50, images10MiB/videos100MiB/total100MiB, argument16KiB), reports filename alt or explicit #alt and hashes, preserves original body with final_body_known:false. No upload URLs or final Markdown are invented. Real mode refuses before body input/network. Parent/sibling parity is separate.'}
        result['pr_create_assignee'] = {'flags': ['--assignee', '-a'], 'values': 'Exact logins or @me, repeated/comma-separated, case-insensitive deduplication; at most 100 names', 'auth': 'Public PAT only; no cookies', 'verification': 'Resolve positive user IDs and canonical logins, pin @me lookup identity, verify exact ID/login set on independent metadata and final PR readbacks', 'dry_run': 'Offline unresolved names; no existence/eligibility prediction', 'limits': 'Creation timeout60 and complete responses32MiB; server enforces assignability; no atomic CAS, idempotency or automatic mutation retry'}
        result['pr_create_project'] = {'flags': ['--project', '-p'], 'status': 'offline_plan_only', 'behavior': 'Project titles, repeated or CSV with quoted commas; preserve literal case/whitespace/order/repeats, never treat numeric titles as IDs. At most100 titles, each nonblank UTF-8 without C0/DEL/C1 controls and <=1024 bytes; each input <=1MiB.', 'dry_run': 'No network/auth/editor/push. projects entries retain unresolved title, id:null, assignment_supported:null. metadata_resolved:false, final_request_known:false; no guessed project wire field.', 'live': 'feature_unavailable before body/editor/HTTP/creation. Internal read-only ProjectResolver is not wired into creation: two complete exact-title catalog scans, repository identity recheck, timeout/size/page/entry bounds; no assignment or permission-to-write inference. Official Kanban docs explicitly limit tasks to one project, without establishing PR membership.', 'remaining': 'Establish PR assignment and current-membership readback, resolve multiple-project platform difference, integrate resolver with preflight/assignment/verification and write recovery; fresh positive installed worker/verifier evidence. Read-only bulk-values probe returned403, which does not prove platform absence. Parent/sibling parity separate.'}
        result['pr_create_milestone'] = {'flags': ['--milestone', '-m'], 'status': 'offline_plan_only', 'behavior': 'Literal name, including numeric names; never interpreted as an ID. Nonblank UTF-8 without C0/DEL/C1 controls, at most1024 bytes.', 'dry_run': 'No network/auth; name unresolved, id:null, assignment_supported:null; metadata_resolved:false and final_request_known:false. No milestone wire ID guessed.', 'live': 'feature_unavailable before input/editor/network/write; name-to-ID catalog and PR assignment contract unimplemented. Public creation schema has integer milestone, but official object documentation says always null; PR POST examples conflict. This does not prove platform absence.', 'remaining': 'Establish scoped name lookup and assignment/readback contracts, implement bounded preflight and verification, fresh installed positive worker/verifier creation. Parent/sibling parity separate.'}
        result['pr_create_reviewer'] = {'flags': ['--reviewer', '-r'], 'values': 'Reviewer logins, ORG/TEAM-SLUG (exact visible slug) or team:ID; repeated/CSV, deduplicated IDs, at most100 handles/1MiB input', 'auth': 'Separate public PAT and cookie-only web session; matching public id/login and web id/username required; author cannot review own PR', 'verification': 'Resolve/recheck handles, scoped candidate eligibility, one public creation, scoped PR/reviewer precheck before each web attach, exact reviewer sets after each attach and final GET', 'bounds': 'Shared timeout60/complete32MiB responses; team catalogs to empty <=100 pages of100; candidates single documented frontend GET <=10000 per kind', 'dry_run': 'Offline unresolved handles, no auth/HTTP/push; no eligibility or final request prediction', 'recovery': 'No retry/CAS/rollback; retain PR IDs, pending reviewer ID/type and completed steps; unknown attach stays unknown even after confirmed creation', 'remaining': 'Positive installed reviewer creation and independent verification require authorized web cookies and isolated eligible people/team fixtures; frontend DTOs remain live-unverified; no Copilot/re-request or team membership inference; parent/siblings separate'}
        result['pr_create_label'] = {'flags': ['--label', '-l'], 'values': 'Existing repository label names; repeated/comma-separated, Unicode simple case-insensitive resolution and ID deduplication; at most100 names,40 characters each. Ambiguous, unknown, archived or conflicting exclusive scopes refuse.', 'auth': 'Public PAT only; no cookies', 'verification': 'Resolve and recheck ID/name/archive/exclusive state before one POST; exact ID/canonical-name sets verified on independent metadata and final PR GETs', 'dry_run': 'Offline unresolved names; no label IDs, existence/permission prediction or final request claim', 'bounds': 'Each of two catalogs reads to empty page, <=100 pages and100 entries/page; shared timeout60 and complete responses32MiB', 'recovery': 'No no-op/idempotency/CAS/rollback or automatic retry; retain repository/head/base/stage and known PR IDs. Reconcile before another creation.', 'remaining': 'Already published branches required; reviewers use separate cookie-only web API; parent/sibling parity separate.'}
        result['pr_create_body_file'] = {'flags': ['--body-file', '-F'], 'behavior': 'Literal UTF-8 regular file or piped stdin with -; preserve CR/LF, spaces, Unicode and empty text. Mutually exclusive with --body. No editor or prompt.', 'bounds': '1048576 input bytes, no NUL; reject symlinks, nonregular files, changed file snapshots and TTY stdin. Shared timeout60 includes idle pipes; response32MiB. No pagination for this flag.', 'verification': 'Offline dry-run reads and validates input without network; metadata identities unresolved. Public PAT only, one POST201 and independent exact-body/identity GET.', 'recovery': 'Every invocation creates anew; no no-op/idempotency/CAS or automatic retries. Errors retain scope, stage, mutation_attempted and known PR IDs; reconcile before another create.', 'remaining': 'Existing same-repository remote branches required; draft title normalization, attachment, template/editor/browser and full parent parity remain separate.'}
        result['pr_create_body'] = {'flags': ['--body', '-b'], 'behavior': 'Literal UTF-8 PR description; preserve spaces, CR/LF, Unicode and Markdown; empty/whitespace allowed. Omission leaves body unset. Mutually exclusive with --body-file. No input, editor or expansion.', 'bounds': 'At most1048576 UTF-8 bytes; invalid Unicode and NUL refuse before network. Shared timeout60 and complete response32MiB; this flag needs no pagination.', 'verification': 'Public PAT only; one POST201 and independent GET check exact body, PR/author/repository/ref/SHA identity. Dry-run is offline and preserves literal body, with metadata identities unresolved.', 'recovery': 'No no-op/idempotency/CAS or automatic retry; errors retain scope, mutation_attempted and known PR IDs. Reconcile before another create.', 'remaining': 'Existing remote branches required; template/editor/browser/attachment and parent parity remain separate.'}
        result['pr_create_draft'] = {'flags': ['--draft', '-d'], 'behavior': 'Create a draft PR using public draft:true and an explicit Draft: title prefix unless an existing case-insensitive draft[:space:]: marker is present. Exact wire title and boolean draft verified on POST201 and independent GET.', 'dry_run': 'Offline exact request including normalized title; no network, push, PR ID or eligibility prediction.', 'bounds': 'Shared timeout60, complete response32MiB, normalized title1MiB; no pagination for this flag.', 'recovery': 'Single create, never a no-op, automatic retry or rollback. Scope/stage/known PR IDs retained on errors; reconcile before another create. No atomic CAS.', 'remaining': 'Existing same-repository remote branches required. Native draft titles carry a visible prefix. Reviewer cookie API and sibling/parent parity remain separate.'}
        result['pr_create_head'] = {'flags': ['--head', '-H'], 'selection': 'Explicit exact existing branch or USER:BRANCH; omission uses bounded symbolic HEAD; empty input refuses. Same-owner qualification verifies User identity. Cross-owner resolves one visible fork in the same verified ancestry containing the exact branch, including renamed forks.', 'source_scope': 'Authenticated owner uses user/repos including private repositories; other owners use public-only users/USER/repos. Third-party source mutation refuses feature_unavailable: documented head_repo has no owner field and this mapping remains unverified.', 'dry_run': 'No HTTP/auth/editor/push; explicit/configured base required. Cross-owner source and head_repo unresolved, final_request_known:false; no guessed source name or PR ID.', 'bounds': 'timeout60 shared, Git4MiB/read, head1024 UTF-8 bytes, complete HTTP32MiB; each of two resolution scans <=1000 GETs,100000 entries,100 pages/catalog,100 entries/page (oversized responses refuse),10 ancestor levels. Read to empty page; malformed/ambiguous/limited catalogs refuse.', 'verification': 'Single public PAT POST201 with documented head_repo repository-name field for authenticated-owner fork; reselect before POST, pin source/base IDs, refs and SHAs separately, verify independent GET. Local fill pins must match source/base. Same ref name allowed across distinct repositories. No no-op/idempotency/CAS/retry; retain known PR IDs/source and explicit mutation_outcome.', 'remaining': 'Cross-owner mapping remains unverified live; third-party source writes intentionally unavailable until its owner mapping is established. Fresh positive installed-package worker and independent verifier cross-owner creation required. Earlier HTTP400 probes are inconclusive. Only --head is in scope; parent/sibling parity separate.'}
        result['pr_create_base'] = {'flags': ['--base', '-B'], 'selection': 'Explicit target branch, else branch.<current>.gh-merge-base effective Git config, else verified repository default_branch; current means checkout branch even with --head', 'scope': 'Existing same-repository branch, exact case-sensitive name including slashes; no tag/SHA/fork resolution or push', 'dry_run': 'Offline; explicit base or current branch config required; no branch existence or PR ID prediction', 'verification': 'POST201 and independent GET must match base ref, repository ID/full_name and SHA; no atomic CAS or automatic mutation retry', 'bounds': 'Shared input/API/config timeout60; complete HTTP responses32MiB; config is read-only; no branch catalog pagination'}
        result['item_search'] = {'commands': ['search prs', 'search issues'], 'scope': 'Repeated --repos OWNER/REPO or single -R/context; no global index', 'filters': 'Literal title/body substring, state, author/assignee login or @me, all requested labels; AND metadata filters', 'coverage': 'All selected catalogs to empty page before output; aggregate limits and shared deadline; no partial result on inaccessible repo or malformed/unknown required metadata; issue search excludes PR discriminator', 'limits': ['max-repos default20 <=100', 'max-pages per repo', 'max-entries/matches aggregate', 'page-size', 'timeout'], 'verification': 'PR matches and issue PR-exclusion live; matching pure issue responses contract-tested only'}
        result['pr_checkout'] = {'worktree': 'New nonempty destination path, relative to current directory or absolute; existing paths/symlinks refuse. Uses --branch or head.ref, or --detach. Existing branch ff/noop, explicit --force permits reset; branches in other worktrees remain protected. Verifies destination SHA/ref and original HEAD SHA/branch; original_head_changed returns recovery details. already_current describes the branch, while the new worktree is still created. No atomic CAS/rollback.', 'branch': 'Local branch name via --branch/-b; nonempty valid Git name, defaults to head.ref; mutually exclusive with --detach. Final HEAD and full branch ref verified.', 'operation': 'Read a PR by number/URL, fetch its pinned head SHA from the source repository and switch locally; no remote writes. Branch/current selectors require one unique open PR.', 'required_inputs': 'Matching clean Git clone and explicit PR number/URL, or a resolvable unique open branch/current selector', 'flags': ['--branch', '--detach', '--force', '--worktree', '--timeout'], 'backend': 'public PAT + scoped HTTPS Git helper', 'safety': 'Clean matching clone; pinned SHA and PR identity recheck. Existing branches fast-forward unless explicit --force. New worktree path only; checked-out branches protected by Git.', 'recovery': 'Local failures retain previous HEAD/target SHA and destination; inspect state before retry, no automatic rollback or API writes', 'limits': 'No recursive submodule update or automatic upstream tracking. --force has no effect with --detach. Checkout timeout starts after selector resolution; branch lookup has separate pagination bounds.'}
        result['pr_create'] = {'mode': 'Existing same-repository branches; --title, --recover, --editor, --fill, --fill-first or --fill-verbose required; optional literal body/file/stdin, no positional arguments', 'verification': 'Public PAT repo/current-user preflight, one POST201 and independent PR GET; complete UTF-8 JSON <=32MiB, reject EOF before declared Content-Length even when JSON parses', 'bounds': 'Shared timeout60, title/body <=1MiB; offline dry-run requires explicit/configured base and does not resolve metadata identities', 'recovery': 'No retry/idempotency/CAS; on attempted write reconcile recorded pull_number/pull_id or scoped head/base catalog before invoking again; truncated POST may leave IDs unknown, truncated readback retains accepted IDs', 'remaining': 'Separate cookie-only reviewer API is contract-tested; template/browser/milestone/project/push/fork/full parent parity are separate'}
        result['review_context'] = {'command': 'pr context SELECTOR [--require-patches]', 'backend': 'public PAT, read-only', 'output': 'Existing pull_request/files/commits/reviews/comments plus head_sha/base_sha/missing_patches/coverage', 'verification': 'PR ID/branch/head/base/state/version rechecked; files count matched; paginated catalogs to empty, discussion comments documented unpaginated', 'bounds': ['timeout120', 'max-pages100 per catalog', 'page-size <=100', 'max-entries100000 combined', '32MiB per response'], 'limits': 'No atomic discussion snapshot, inline thread completeness, full patch integrity or approval/CI decision; use head_sha as review --commit'}
        result['discussion_comment_body_file'] = {'flags': ['--body-file FILE', '-F FILE', '--body-file -'], 'behavior': 'Read literal UTF-8 from a regular file or piped stdin; preserve CR/LF, spaces and Unicode. Mutually exclusive with --body. No editor, prompt or expansion; every invocation appends.', 'bounds': '1048576 input bytes; nonblank, no NUL; reject symlinks/nonregular files and changed file snapshots. Stdin TTY refuses; idle pipe reads share timeout60 after selector resolution.', 'verification': 'Public PAT discussion creation: one POST then independent GET checking exact text, author and parent. Dry-run reads and validates input with no network.', 'recovery': 'No retry/CAS/deduplication. On uncertain write inspect comment_id when known or parent discussion before retrying.', 'remaining': 'Inline/reply retain existing behavior; attachment and other optional siblings and full parent parity remain separate. Shared branch/current selector bounds are documented in discussion_comment_create.'}
        result['discussion_comment_body'] = {'flags': ['--body TEXT', '-b TEXT'], 'behavior': 'Literal discussion text; preserves whitespace, newlines, Unicode and Markdown without shell expansion. Mutually exclusive with --body-file. Every invocation appends, even equal text.', 'input': 'Nonblank valid UTF-8, at most 1048576 bytes, no NUL; invalid Unicode returns JSON usage_error before discussion network. No editor or prompt.', 'verification': 'Single POST and independent GET verifies exact text, author and parent; offline dry-run sends nothing', 'remaining': 'Optional attachment behavior and parent parity remain separate; web browser handoff is documented separately; inline/reply paths retain their existing behavior'}
        result['discussion_comment_create'] = {'command': 'pr comment [NUMBER|URL|HEAD] --body TEXT|--body-file FILE/-', 'auth': 'Public PAT, current user and PR repository checked; server enforces comment permission', 'verification': 'PR revision rechecked, single POST, independent GET by ID verifies author/parent/exact text; original DTO returned', 'bounds': '1 MiB UTF-8 input without NUL; timeout60 after selector resolution; 32 MiB per discussion/preflight response after selector resolution; no discussion pagination needed', 'limits': 'Every invocation creates a new comment, including equal text. No automatic retries or atomic CAS; recover using comment_id/parent discussion after errors. Shared branch/current selection retains100-page/per-request-time limits, no separate byte cap/overall deadline or low-level HTTPException normalization guarantee; number/URL is the verified normal mode. Editor input is documented separately; web browser handoff is documented separately; delete-last has its own native-cascade contract; edit-last with create-if-none is documented separately. Inline/reply paths retain their existing behavior; offline dry-run has no network'}
        result['discussion_comment_web'] = {'flags': ['--web', '-w'], 'behavior': 'Open canonical PR discussion in a browser to write; --edit-last opens newest own public discussion via native #comment-ID; --create-if-none permits new discussion on own-comment absence. CLI never submits text.', 'exclusions': 'Reject body/body-file/editor/attachment/delete-last/yes/inline/reply/commit before lookup; public PAT preflight only, never cookie API.', 'browser': 'GV_BROWSER shell-style argv without shell; default /usr/bin/open on macOS or xdg-open. Private credential env removed, null stdio, shared timeout60. Success acknowledges launcher only, not UI sign-in/focus/edit/submission.', 'bounds': 'Public complete responses32MiB; timeout60 across HTTP and launcher, PR selector100 pages/max-entries100000; own-last complete unpaginated catalogs validated twice with PR/current-user/comment identity checks. Local Git context helpers retain per-process bounds.', 'dry_run': 'No browser, input, editor, network or write; existence/selected comment remain unknown. Numeric new-mode canonical URL is unverified.', 'limits': 'No GitHub new-form fragment assumed, no guaranteed form focus or automatic edit activation. Browser uses its own login session. Snapshot checks are not atomic; no automatic launcher retry or API mutation. Fresh real-browser positive evidence and independent verifier still required.'}
        result['discussion_comment_editor'] = {'flags': ['--editor', '-e'], 'behavior': 'Open configured text editor directly without prompts; empty new draft or newest own body with --edit-last; --create-if-none permits empty fallback draft. Mutually exclusive with body/body-file/delete-last; discussion only.', 'editor': 'GV_EDITOR then VISUAL then EDITOR then vi; shlex argv, no shell; Markdown file appended. TTY used when available, otherwise null stdio for automated editors. No editor output enters JSON streams.', 'bounds': 'Shared timeout60 after selector resolution includes editor; private temporary file, UTF-8 <=1MiB without NUL/nonblank; credential environment variables removed from editor. Editor failure/timeout/invalid draft sends no write.', 'dry_run': 'No editor, temporary draft, network or write; body and selected ID remain unknown.', 'verification': 'Existing public PAT snapshot/recheck/single-write/independent readback and recovery guards; no atomic CAS or retry. Attachment creation retains its experimental status; inline/reply refuse; web and full parent parity remain separate.'}
        result['discussion_comment_edit'] = {'commands': ['pr comment-edit', 'issue comment-edit'], 'selector': 'Parent selector and required --comment-id ID; --body TEXT or --body-file FILE/-', 'auth': 'Public PAT; own discussion comments only', 'verification': 'Parent/current user/author/issue_url checked, equal-body noop, recheck body/version and parent, single PATCH followed by GET; no automatic mutation retries or atomic CAS', 'limits': '1 MiB UTF-8, no NUL, timeout60; explicit-ID discussion edits only; last-own deletion is documented separately with native cascade; dry-run has no network'}
        result['pull_request_create'] = {'command': 'pr create -R OWNER/REPO --head BRANCH --base BASE [--title TITLE|--recover FILE|--editor|--fill|--fill-first|--fill-verbose] [--body TEXT|--body-file FILE/-]', 'mode': 'Existing same-repository remote branches; title, --recover, --editor, --fill, --fill-first or --fill-verbose; no positional arguments; no push or fork', 'auth': 'Public PAT only; selected repository ID/full_name and current author verified before POST; server enforces permission. Reviewer options retain separate cookie-only web auth.', 'verification': 'One POST requiring HTTP201; validate PR identity, author, text/state/draft, both repository scopes/refs/SHAs, then independent GET with matching ID/number/SHAs. No atomic snapshot or automatic mutation retry.', 'bounds': 'Shared timeout60 for input and public/web requests; complete UTF-8 JSON responses <=32MiB; title/body <=1MiB, body-file regular UTF-8 or piped stdin; metadata labels <=100 pages of100', 'dry_run': 'Offline request; explicit base required; metadata names remain unresolved. No push, network or predicted PR ID.', 'recovery': 'Errors retain repository/head/base/pull_number/pull_id when known/stage/mutation_attempted. Reconcile by ID or scoped head/base catalog before deciding to retry; create is not idempotent.', 'limits': 'No implicit template application/interactive catalog chooser, push/fork, milestone/project, browser or full gh output parity. Reviewer candidate eligibility and cookie-backed live writes retain their existing limitations; metadata steps are not atomic.'}
        result['discussion_comment_yes'] = {'flag': '--yes', 'requires': '--delete-last', 'behavior': 'Skip deletion confirmation; gv never prompts, so --yes is required for an actual --delete-last write. It does not select another comment, override ownership/permissions/bounds, or retry a write.', 'dry_run': 'Optional with --dry-run; same offline selection plan with or without --yes, no network or predicted comment ID.', 'recovery': 'One DELETE204 then independent GET404. No absent-comment no-op or automatic retry; after uncertainty reconcile recorded comment_id and discussion before any new deletion.', 'limits': 'Newest own public discussion only; GitVerse native thread-parent deletion cascades replies. No atomic CAS. Other comment modes reject --yes before selector lookup.'}
        result['discussion_comment_delete_last'] = {'command': 'pr comment SELECTOR --delete-last --yes', 'behavior': 'Delete newest own public discussion comment by created_at then greatest ID; absence is an error. No text or inline/reply/attachment/edit combinations.', 'auth': 'Public PAT only; PR identity/revision, current user, every catalog parent/author and selected comment rechecked', 'verification': 'Documented unpaginated catalog read twice; one DELETE requires empty HTTP204 then independent GET404; no retries or atomic CAS', 'bounds': 'timeout60 after selector resolution; complete32MiB responses; max-entries100000 per catalog; shared branch selectors have separate100-page/per-request timeout bounds', 'dry_run': 'Offline select DELETE plan without predicted comment ID; --yes only required for writes. No prompt.', 'limits': 'GitVerse cascades replies when deleting a thread parent; public comment DTO cannot prove reply absence. No guarantee of reply preservation. Final-preflight races remain possible. After uncertainty read recorded comment ID and discussion; never blindly rerun delete-last, which can delete an older comment.'}
        result['discussion_comment_edit_last'] = {'command': 'pr comment SELECTOR --edit-last --body TEXT|--body-file FILE/-', 'behavior': 'Edit newest own public discussion by created_at then greatest ID; other authors and updated_at do not select it. Equal text returns changed:false; absence errors unless --create-if-none.', 'auth': 'Public PAT only; no cookies or returned URL requests; server enforces edit permission', 'verification': 'PR identity/revision, current user, every catalog author/parent and selected-ID GET checked; complete unpaginated catalog read twice before one PATCH and independent exact-text/author/parent GET', 'bounds': 'timeout60 after selector resolution, response32MiB, max-entries100000 per catalog, body1MiB UTF-8; incomplete/malformed/ambiguous/bound-exceeded results fail', 'dry_run': 'Offline selection plan, unknown comment ID and existence; never predicts a write. Branch/current selectors retain shared selection-only plan and separate100-page/per-request timeout bounds.', 'limits': 'Required nonblank body/file/stdin; attachment/inline/reply/commit/delete-last/yes combinations refuse before selection. No atomic CAS or write retry; final-preflight races possible. Uncertain write/readback errors retain repository/number/comment_id/stage/mutation_attempted for read-only reconciliation. Editor input is documented separately; web and full parent parity remain separate.'}
        result['discussion_comment_create_if_none'] = {'command': 'pr comment SELECTOR --edit-last --create-if-none --body TEXT|--body-file FILE/-', 'behavior': 'Create once only when no own public discussion comment exists; otherwise edit newest own created_at, then greatest ID. Equal text is a no-op. This is not text deduplication.', 'auth': 'Public PAT only, current user/PR repository and revision checked; every catalog entry parent and author validated without following returned URLs', 'verification': 'Complete documented unpaginated catalog read twice; selected comment GET and parent recheck; one POST or PATCH followed by independent exact text/author/parent GET; no atomic CAS or automatic write retry', 'bounds': 'max-entries100000 per catalog, response32MiB, timeout60 after selector resolution, body1MiB UTF-8; incomplete/ambiguous/bound-exceeded catalogs refuse, never imply absence', 'dry_run': 'Offline conditional PATCH/POST plan; selection and comment ID are unknown. Branch selectors retain shared selection-only dry-run and separate lookup bounds.', 'limits': 'Reject without --edit-last or with attachment/inline/reply/commit modes before selection. Cannot combine with delete-last/yes; editor input is documented separately; web remains separate. A concurrent comment after final preflight may race. Recover using repository/number/comment_id/stage/mutation_attempted before deciding to retry.'}
        result['discussion_comment_attach'] = {'command': 'pr comment NUMBER --attach FILE[#ALT] (repeat up to 50)', 'status': 'experimental; incomplete; installed live uploads returned422, not a proven unsupported platform difference', 'auth': 'Public PAT only; own newly created discussion; no inline/reply upload, cookies or redirects', 'bounds': 'Images10MiB, videos100MiB, total100MiB buffered; body1MiB; timeout60 after selector resolution, response32MiB; regular files only, no symlinks/stdin/globs', 'behavior': 'Optional body/file/stdin; safe inline references and used image/video link reference definitions rewritten, other attachments appended; body-file directory then cwd; escaped alt or filename stem; video bare URL; dry-run validates local files with placeholder URLs and no network', 'recovery': 'Comment created before upload, including pending placeholder for attach-only; every invocation creates anew. Single POST per file, GET metadata readback, recheck then one body PATCH/GET; not atomic/CAS or checksum verification. Inspect comment_id, attachment_ids, returned_attachment_ids, attachment_index and stage; never retry blindly', 'remaining': 'Resolve real422 upload validation; fresh positive image/video installed evidence and independent verifier; full CommonMark block semantics (general HTML, lazy/nested list continuations) remain incomplete; nested labels, multiline definitions/code spans and explicit quote/list fences have contract coverage; limits128 label nesting/32 destination nesting/10000 references; reference-style video images refuse before network, ordinary video links are supported. GitVerse player rendering unverified'}
        result['writes'] += ' Local config set/unset and alias set/import/delete persist private settings. Alias invocation uses normal command auth and mutation checks.'
        return result
    if args.group == "traffic":
        return traffic_inventory(args.files)
    if args.group == "api" and args.action == "endpoints":
        return endpoints(args.search, getattr(args, "backend", "public"))
    if args.group == "api" and args.action == "schema":
        if getattr(args, "backend", "public") != "public":
            raise GVError("Only public API has a bundled schema; web endpoints have static evidence metadata", "backend_unsupported")
        return schema(args.path, args.method)
    if args.group == 'repo' and args.action == 'delete':
        from .repo_delete import validate_delete_args
        validate_delete_args(args)
    if args.group == 'pr' and args.action == 'create' and args.web:
        from .pr_create_web import validate_web_args
        validate_web_args(args)
    client = Client(getattr(args, "backend", "public"), getattr(args, "dry_run", False))
    if args.group == 'pr' and args.action == 'comment' and args.web:
        from .comment_web import web_comment
        return web_comment(client, args)
    if args.group in ('org', 'team'):
        require_public(client, args.group)
        from .directory import directory_command
        return directory_command(client, args)
    if args.group in ('secret', 'variable'):
        require_public(client, args.group)
        from .actions_config import config_command, config_scope
        scope = config_scope(args)
        return config_command(client, scope, args)
    if args.group == 'release':
        require_public(client, 'release')
        from .releases import release_command
        return release_command(client, repository(args), args)
    if args.group in ('issue', 'label'):
        require_public(client, args.group)
        from .issues import issue_command, issue_target, label_command
        if args.group == 'issue':
            issue_target(args)
            if args.action == 'comment-edit':
                from .comment_edit import edit_comment
                return edit_comment(client, repository(args), args)
            return issue_command(client, repository(args), args)
        return label_command(client, repository(args), args)
    if args.group == 'search':
        require_public(client, 'search')
        from .search import search_code, search_repos
        if args.action == 'repos':
            if getattr(args, 'repo', None): raise GVError('search repos uses --owner, not -R', 'usage_error')
            return search_repos(client, args)
        if args.action == 'commits':
            from .commit_search import search_commits
            return search_commits(client, repository(args), args)
        if args.action in ('prs', 'issues'):
            from .item_search import search_items
            return search_items(client, args)
        return search_code(client, repository(args), args)
    if args.group == "api":
        if args.input and args.field:
            raise GVError("Use --input or --field, not both", "usage_error")
        body = read_json(args.input) if args.input else fields(args.field) if args.field else None
        if args.method in ("GET", "HEAD") and body is not None:
            raise GVError("GET/HEAD body is unsupported; use --query", "usage_error")
        endpoint = args.endpoint
        if any(x in endpoint for x in ("{owner}", "{repo}", "{ownerName}", "{repoName}")):
            owner, repo = repository(args).split("/")
            endpoint = endpoint.replace("{owner}", owner).replace("{repo}", repo).replace("{ownerName}", owner).replace("{repoName}", repo)
        return client.request(args.method, endpoint, fields(args.query), body, args.raw)
    if args.group == "auth":
        require_public(client, "auth")
        if args.action == "status":
            return {"backend": "public", "authenticated": bool(client.auth_source), "source": client.auth_source, "user": client.request("GET", "user")}
        if args.action == "logout":
            if client.dry_run:
                return {"action": "remove_saved_token", "path": str(config_path())}
            config_path().unlink(missing_ok=True)
            return {"removed_saved_token": True, "note": "Environment tokens remain active"}
        if client.dry_run:
            return {"action": "validate_and_save_token", "path": str(config_path())}
        token = sys.stdin.read().strip()
        if not token or "\n" in token or "\r" in token:
            raise GVError("Expected a single token on stdin", "usage_error")
        client.headers["Authorization"] = "Bearer " + token
        user = client.request("GET", "user")
        target = config_path()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.is_symlink():
            raise GVError("Auth path must not be a symlink", "insecure_auth_file")
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump({"api_url": validate_base(client.base), "token": token}, f)
        return {"authenticated": True, "login": user.get("login"), "config": str(target)}
    if args.group == "repo" and args.action == "list":
        require_public(client, "repo list")
        return client.pages("user/repos", {"page": args.page}, args.paginate, min(args.limit, 100))
    if args.group == 'repo' and args.action in ('clone', 'create', 'fork'):
        require_public(client, 'repo ' + args.action)
        from .repositories import repo_command
        return repo_command(client, args)
    if args.group == 'pr' and args.action not in ('create', 'list', 'status'):
        from .checkout import checkout_pr, pr_reference
        if args.action == 'checkout':
            from .checkout import valid_worktree
            valid_worktree(args.worktree)
        selector = args.selector
        if selector is not None and (selector.isdecimal() or '://' in selector or selector.startswith('-')):
            number, url_repo = pr_reference(selector, getattr(args, 'repo', None) or os.environ.get('GV_REPO'))
            if url_repo:
                args.repo = url_repo
            repo = repository(args)
        else:
            if args.action == 'checkout' and args.branch is not None:
                from .checkout import valid_branch
                valid_branch(args.branch)
            from .pulls import resolve_branch
            repo = repository(args)
            selection_client = client if client.backend == 'public' else Client('public', client.dry_run)
            number = resolve_branch(selection_client, repo, selector)
            if client.dry_run:
                return {'action': args.action, 'backend': client.backend, 'repository': repo, 'selection': number,
                        'note': 'Selection is unresolved in offline dry-run. Use a number/URL for a concrete operation request.'}
        args.number = number
        if args.action == 'ready':
            from .ready import pr_ready
            return pr_ready(args, client, repo, number)
        if args.action == 'checks':
            from .checks import pr_checks
            return pr_checks(args, client, repo, number)
        if args.action == 'checkout':
            return checkout_pr(args, client, repo, number)
        return run_pr(args, client, repo)
    repo = repository(args)
    if args.group == "pr":
        if args.action == 'status':
            from .pulls import pr_status
            return pr_status(args, client, repo)
        return run_pr(args, client, repo)
    if args.group == "repo":
        if args.action == 'delete':
            require_public(client, 'repo delete')
            from .repo_delete import delete_repository
            return delete_repository(client, repo, args)
        if args.action in ('archive', 'unarchive', 'rename'):
            require_public(client, 'repo ' + args.action)
            from .repo_lifecycle import lifecycle
            return lifecycle(client, repo, args)
        if args.action == 'edit':
            require_public(client, 'repo edit')
            from .repo_settings import edit_repository
            return edit_repository(client, repo, args)
        if args.action in ('contents', 'read-file', 'read-dir'):
            require_public(client, 'repo ' + args.action)
            from .contents import read_contents
            return read_contents(client, repo, args)
        return client.request("GET", f"repos/{repo}")
    if args.group == 'workflow':
        require_public(client, 'workflow')
        from .workflows import workflow_command
        return workflow_command(client, repo, args)
    if args.group == "run":
        require_public(client, "run")
        if args.action == 'download':
            from .artifacts import download_artifacts
            return download_artifacts(client, repo, args)
        if args.action == 'watch':
            from .run_watch import run_watch
            return run_watch(client, repo, args)
        if args.action == 'view' and (args.log or args.log_failed or args.job):
            from .run_logs import run_logs
            return run_logs(client, repo, args)
        path = f"repos/{repo}/actions/runs"
        if args.action == "list":
            from .run_list import list_runs
            return list_runs(client, repo, args)
        path += "/" + str(args.run_id)
        if args.action in ("cancel", "rerun"):
            return client.request("POST", path + "/" + args.action)
        return client.request("GET", path + ("/jobs" if args.action == "jobs" else ""))
    raise GVError("Unknown command", "usage_error")


def select_fields(value, names):
    if isinstance(value, list):
        return [select_fields(v, names) for v in value]
    if isinstance(value, dict):
        return {k: value[k] for k in names if k in value}
    return value


def main(argv=None):
    try:
        argv = list(sys.argv[1:] if argv is None else argv)
        from .settings import expand_aliases
        argv = expand_aliases(argv)
        # gh-style `gv api repos/...` shorthand alongside offline api subcommands.
        i = 0
        while i < len(argv) and argv[i].startswith('-'):
            option = argv[i].split('=', 1)[0]
            i += 2 if option in ('-R', '--repo', '--backend', '--fields') and '=' not in argv[i] else 1
        if i < len(argv) and argv[i] == 'api':
            if len(argv) > i + 1 and not argv[i + 1].startswith("-") and argv[i + 1] not in ("request", "schema", "endpoints"):
                argv.insert(i + 1, "request")
        args = parser().parse_args(argv)
        result = execute(args)
        from .contents import RawContents
        if isinstance(result, RawContents):
            sys.stdout.buffer.write(result.data)
            sys.stdout.buffer.flush()
            return
        from .checks import ChecksResult
        exit_code = 0
        if isinstance(result, ChecksResult):
            exit_code, result = result.exit_code, result.payload
            if isinstance(getattr(args, 'json', None), str) and not getattr(args, 'dry_run', False):
                from .checks import export_checks
                metadata = {k: v for k, v in result.items() if k != 'checks'}
                print(json.dumps({'checks_metadata': metadata}, ensure_ascii=False), file=sys.stderr)
                result = export_checks(result, args.json)
        if getattr(args, "fields", None):
            result = select_fields(result, args.fields.split(","))
        if isinstance(result, str) and (getattr(args, "patch", False) or getattr(args, "raw", False)):
            print(result, end="" if result.endswith("\n") else "\n")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if exit_code:
            raise SystemExit(exit_code)
    except GVError as e:
        print(json.dumps(e.as_dict(), ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2 if e.code in ("usage_error", "invalid_repo", "repo_required", "invalid_path", "invalid_url") else 1)
    except BrokenPipeError:
        raise SystemExit(0)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
        # Do not echo JSON parse errors or file contents that might contain secrets.
        error = GVError("Invalid input, unreadable file or failed local operation", "input_error", details={"type": type(e).__name__})
        print(json.dumps(error.as_dict()), file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
