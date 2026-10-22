"""Private input restoration, never automatic replay of an attempted PR write."""
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from .client import GVError

MAX_BYTES = 16 * 1048576
FIELDS = ('title', 'body', 'head', 'base', 'draft', 'no_maintainer_edit',
          'assignee', 'label', 'reviewer', 'milestone', 'project')


def check_time(deadline):
    if time.monotonic() >= deadline:
        raise GVError('Recovery input deadline reached', 'deadline_exceeded')


def fingerprint(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode


def private(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
            or info.st_uid != os.getuid() or info.st_nlink != 1):
        raise GVError('Recovery input must be a private, singly linked regular file owned by this user (0600)', 'invalid_recovery')


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


class Recovery:
    def __init__(self, args, repo, client, deadline):
        self.args, self.repo, self.client, self.deadline = args, repo, client, deadline
        self.source = None
        self.path = None
        self.state = None
        self.snapshot = None
        self.base = getattr(client, 'base', 'https://api.gitverse.ru')

    def __enter__(self):
        filename = getattr(self.args, 'recover', None)
        if filename is None:
            return self
        try:
            check_time(self.deadline)
            if not isinstance(filename, str) or not filename or filename == '-':
                raise GVError('--recover requires a saved JSON file path, not stdin', 'invalid_recovery')
            self.path = Path(filename).absolute()
            flags = os.O_RDONLY if self.client.dry_run else os.O_RDWR
            fd = os.open(self.path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
            self.source = os.fdopen(fd, 'rb' if self.client.dry_run else 'r+b')
            import fcntl
            try:
                fcntl.flock(self.source, (fcntl.LOCK_SH if self.client.dry_run else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise GVError('Recovery input is already in use', 'recovery_busy') from None
            before = os.fstat(self.source.fileno())
            private(before)
            if before.st_size > MAX_BYTES:
                raise GVError('Recovery JSON exceeds 16 MiB', 'input_limit')
            raw = self.source.read(MAX_BYTES + 1)
            self.snapshot = fingerprint(before)
            self.recheck()
            if len(raw) > MAX_BYTES:
                raise GVError('Recovery JSON exceeds 16 MiB', 'input_limit')
            self.state = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            self.validate()
            if self.state['mutation_attempted'] and not self.client.dry_run:
                raise GVError('Saved run attempted a write; reconcile recorded PR IDs/head/base. --recover restores input, never replays an attempted mutation; use --dry-run to inspect',
                              'recovery_requires_reconciliation', details={k: self.state[k] for k in
                              ('repository', 'mutation_attempted', 'mutation_outcome', 'pull_id', 'pull_number')})
            values = self.state['input']
            for key in FIELDS:
                explicit = getattr(self.args, key, None)
                if key in ('head', 'base') and explicit is not None and values[key] is not None and explicit != values[key]:
                    raise GVError('Recovery branch scope differs from explicit input', 'recovery_scope_mismatch')
                if explicit is None or key in ('draft', 'no_maintainer_edit') and not explicit:
                    value = values[key]
                    # An unfinished fill/editor seed is not an explicit title.
                    if key == 'title' and not value.strip():
                        value = None
                    setattr(self.args, key, value)
            check_time(self.deadline)
            return self
        except (OSError, ValueError, UnicodeError, RecursionError, ImportError):
            self.close()
            raise GVError('Cannot safely read recovery JSON; no input or credentials are echoed', 'invalid_recovery') from None
        except BaseException:
            self.close()
            raise

    def validate(self):
        from .pr_create import branch, validate_body, validate_title, positive_id
        row = self.state
        keys = {'version', 'command', 'repository', 'public_api', 'input', 'mutation_attempted',
                'mutation_outcome', 'pull_id', 'pull_number'}
        if (not isinstance(row, dict) or set(row) != keys or type(row['version']) is not int
                or row['version'] != 1 or row['command'] != 'pr create'
                or not isinstance(row['repository'], str) or not isinstance(row['public_api'], str)):
            raise GVError('Invalid gv PR-create recovery schema/version', 'invalid_recovery')
        if row['repository'].casefold() != self.repo.casefold() or row['public_api'] != self.base:
            raise GVError('Recovery repository or public API scope differs', 'recovery_scope_mismatch')
        values = row['input']
        if (not isinstance(values, dict) or set(values) != set(FIELDS)
                or type(values['draft']) is not bool or type(values['no_maintainer_edit']) is not bool
                or type(row['mutation_attempted']) is not bool
                or row['mutation_outcome'] not in ('none', 'confirmed', 'unknown')
                or (not row['mutation_attempted'] and (row['mutation_outcome'] != 'none' or row['pull_id'] is not None or row['pull_number'] is not None))
                or (row['mutation_attempted'] and row['mutation_outcome'] == 'none')
                or any(row[k] is not None and not positive_id(row[k]) for k in ('pull_id', 'pull_number'))):
            raise GVError('Invalid recovery input or mutation state', 'invalid_recovery')
        validate_title(values['title'], allow_blank=True)
        validate_body(values['body'])
        if values['base'] is not None:
            branch(values['base'])
        # Head may use the already supported USER:BRANCH syntax.
        from .pr_create import select_head
        if values['head'] is not None:
            if not isinstance(values['head'], str):
                raise GVError('Invalid recovery head input', 'invalid_recovery')
            select_head(values['head'], self.repo, self.deadline)
        if row['mutation_attempted'] and (values['head'] is None or values['base'] is None):
            raise GVError('Attempted-write recovery must retain resolved branches', 'invalid_recovery')
        for key in ('assignee', 'label', 'reviewer', 'project'):
            value = values[key]
            if value is not None and (not isinstance(value, list) or not 1 <= len(value) <= 100
                    or any(not isinstance(v, str) or not v or len(v.encode('utf-8')) > 1048576 or '\0' in v for v in value)):
                raise GVError('Invalid recovery metadata input', 'invalid_recovery')
        if values['milestone'] is not None and not isinstance(values['milestone'], str):
            raise GVError('Invalid recovery milestone input', 'invalid_recovery')

    def recheck(self):
        private(os.fstat(self.source.fileno()))
        if (fingerprint(os.fstat(self.source.fileno())) != self.snapshot
                or fingerprint(self.path.lstat()) != self.snapshot):
            raise GVError('Recovery file changed during use', 'concurrent_change')

    def prepare(self, title, body, head, base):
        values = {key: getattr(self.args, key, None) for key in FIELDS}
        values.update(title=title, body=body, head=head, base=base,
                      draft=bool(self.args.draft), no_maintainer_edit=bool(getattr(self.args, 'no_maintainer_edit', False)))
        self.input = values

    def persist_source(self, row):
        self.recheck()
        data = encode(row)
        self.source.seek(0)
        self.source.write(data)
        self.source.truncate()
        self.source.flush()
        os.fsync(self.source.fileno())
        self.snapshot = fingerprint(os.fstat(self.source.fileno()))
        self.state = row
        self.recheck()

    def before_mutation(self):
        if self.source is None:
            return
        check_time(self.deadline)
        # Persist the guard BEFORE the POST. Interrupted/corrupt journals fail
        # closed; another invocation cannot silently reuse a successful draft.
        try:
            self.persist_source(self.document({'mutation_attempted': True, 'mutation_outcome': 'unknown'}))
        except OSError:
            raise GVError('Cannot persist recovery mutation guard; no POST sent', 'recovery_save_failed') from None

    def finish(self, result):
        if self.source is None or self.client.dry_run:
            return
        details = {'mutation_attempted': True, 'mutation_outcome': 'confirmed',
                   'pull_id': result['id'], 'pull_number': result['number'],
                   'repository': self.repo, 'head': self.input['head'], 'base': self.input['base'],
                   'stage': 'finalize_recovery'}
        try:
            self.persist_source(self.document(details))
        except (OSError, GVError):
            raise GVError('PR verified but recovery journal could not be updated', 'recovery_save_failed',
                          details=details) from None

    def document(self, details):
        return {'version': 1, 'command': 'pr create', 'repository': self.repo, 'public_api': self.base,
                'input': self.input, 'mutation_attempted': details.get('mutation_attempted', False),
                'mutation_outcome': details.get('mutation_outcome', 'none'),
                'pull_id': details.get('pull_id'), 'pull_number': details.get('pull_number')}

    def preserve(self, error):
        if self.client.dry_run or not hasattr(self, 'input'):
            return
        details = error.details or {}
        try:
            data = encode(self.document(details))
            fd, filename = tempfile.mkstemp(prefix='gv-pr-create-', suffix='.json')
            try:
                with os.fdopen(fd, 'wb') as target:
                    target.write(data)
                    target.flush()
                    os.fsync(target.fileno())
            except BaseException:
                os.unlink(filename)
                raise
            error.details = {**details, 'recover_file': filename,
                             'recover_replay_allowed': not details.get('mutation_attempted', False)}
        except (OSError, GVError, UnicodeError, ValueError, TypeError, RecursionError):
            error.details = {**details, 'recover_save_failed': True}

    def close(self):
        if self.source is not None:
            self.source.close()
            self.source = None

    def __exit__(self, *unused):
        self.close()


def encode(row):
    data = json.dumps(row, ensure_ascii=False).encode('utf-8')
    if len(data) > MAX_BYTES:
        raise GVError('Recovery JSON exceeds 16 MiB', 'input_limit')
    return data
