"""Private local settings and argv-only aliases; never execute a shell."""
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile

from .client import GVError

DEFAULTS = {'default_repo': None, 'http_timeout': 30.0}
MAX_BYTES, MAX_ALIASES, MAX_TOKENS, MAX_DEPTH = 1048576, 256, 128, 16
NAME = re.compile(r'[a-z][a-z0-9-]*\Z')
POSITION = re.compile(r'\$([1-9][0-9]?)\Z')


def settings_path():
    base = Path(os.getenv('XDG_CONFIG_HOME') or Path.home() / '.config') / 'gitverse-agent-cli' / 'settings.json'
    path = Path(os.getenv('GV_SETTINGS') or base).expanduser().absolute()
    from .client import config_path
    if path == config_path().expanduser().absolute():
        raise GVError('GV_SETTINGS must point to a different file from the authentication GV_CONFIG', 'settings_auth_collision')
    return path


@lru_cache(maxsize=1)
def builtin_names():
    import argparse
    from .cli import parser
    return frozenset(next(a for a in parser()._actions if isinstance(a, argparse._SubParsersAction)).choices)


def normalize(key, value, from_text=False):
    if key not in DEFAULTS: raise GVError('Unknown setting; supported keys: default_repo, http_timeout', 'unknown_setting')
    if key == 'default_repo':
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value) or any(p in ('.', '..') for p in value.split('/')):
            raise GVError('default_repo must be canonical OWNER/REPO; use config unset to clear', 'usage_error')
        return value
    if isinstance(value, bool) or (not from_text and type(value) not in (int, float)):
        raise GVError('http_timeout must be a finite positive number of seconds', 'usage_error')
    try: result = float(value)
    except (TypeError, ValueError, OverflowError) as error: raise GVError('http_timeout must be a finite positive number of seconds', 'usage_error') from error
    if not math.isfinite(result) or result <= 0: raise GVError('http_timeout must be a finite positive number of seconds', 'usage_error')
    return result


def alias_tokens(value):
    if isinstance(value, str):
        if len(value.encode('utf-8')) > MAX_BYTES: raise GVError('Alias definition is too large', 'settings_limit')
        try: value = shlex.split(value, posix=True)
        except ValueError as error: raise GVError('Invalid alias quoting', 'usage_error') from error
    if not isinstance(value, list) or not value or len(value) > MAX_TOKENS or any(not isinstance(v, str) or '\0' in v or len(v.encode('utf-8')) > 8192 for v in value):
        raise GVError('Alias must be a nonempty argv array of at most 128 bounded strings without NUL', 'usage_error')
    if not NAME.fullmatch(value[0]): raise GVError('Alias target must be a gv command or another alias; leading flags/shell aliases are unsupported', 'usage_error')
    numbered, all_args = False, False
    for token in value:
        if token == '$@': all_args = True
        elif POSITION.fullmatch(token): numbered = True
        elif re.search(r'\$(?:[0-9]+|@)', token): raise GVError('Alias placeholders must be standalone $1..$99 or $@ tokens', 'usage_error')
    if numbered and all_args: raise GVError('Use numbered placeholders or $@, not both', 'usage_error')
    return list(value)


def validate(data):
    if not isinstance(data, dict) or set(data) != {'version', 'config', 'aliases'} or type(data['version']) is not int or data['version'] != 1 or not isinstance(data['config'], dict) or not isinstance(data['aliases'], dict):
        raise GVError('Invalid settings schema; expected version 1, config and aliases', 'invalid_settings')
    config = {key: normalize(key, value) for key, value in data['config'].items()}
    aliases = data['aliases']
    if len(aliases) > MAX_ALIASES: raise GVError('Alias count bound reached', 'settings_limit')
    builtins = builtin_names()
    cleaned = {}
    for name, tokens in aliases.items():
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in builtins:
            raise GVError('Alias names must be lowercase ASCII and cannot shadow builtin commands', 'usage_error')
        cleaned[name] = alias_tokens(tokens)
    for name in cleaned:
        target, seen = name, set()
        while target not in builtins:
            if target not in cleaned: raise GVError('Alias target is not a builtin or saved alias', 'alias_target_not_found')
            if target in seen: raise GVError('Alias cycle refused', 'alias_cycle')
            seen.add(target)
            if len(seen) > MAX_DEPTH: raise GVError('Alias chain exceeds 16 levels', 'alias_limit')
            target = cleaned[target][0]
    return {'version': 1, 'config': config, 'aliases': cleaned}


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('duplicate key')
        result[key] = value
    return result


def decode(raw):
    try: return json.loads(raw, object_pairs_hook=no_duplicates, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError) as error: raise GVError('Expected bounded UTF-8 JSON without duplicate keys', 'invalid_settings') from error


def fingerprint(path):
    try: info = path.lstat()
    except FileNotFoundError: return None
    return info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode


def private_file(info):
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or (hasattr(os, 'getuid') and info.st_uid != os.getuid()):
        raise GVError('Settings/lock must be a regular file owned by the current user with permissions 0600', 'insecure_settings_file')


def read_settings(path):
    try: descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError: return {'version': 1, 'config': {}, 'aliases': {}}, None
    except OSError as error: raise GVError('Cannot safely read settings; file symlinks are refused', 'insecure_settings_file') from error
    with os.fdopen(descriptor, 'rb') as source:
        before = os.fstat(source.fileno()); private_file(before)
        if before.st_size > MAX_BYTES: raise GVError('Settings exceed 1 MiB', 'settings_limit')
        raw = source.read(MAX_BYTES + 1)
        after = os.fstat(source.fileno())
    if len(raw) > MAX_BYTES: raise GVError('Settings exceed 1 MiB', 'settings_limit')
    snap = before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode
    if snap != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode) or fingerprint(path) != snap:
        raise GVError('Settings changed during read', 'concurrent_settings_change')
    return validate(decode(raw)), snap


def load_settings(): return read_settings(settings_path())[0]


def effective(key, data=None):
    data = load_settings() if data is None else data
    if key not in DEFAULTS: normalize(key, None)
    if key == 'default_repo' and os.getenv('GV_REPO'):
        from .cli import parse_repo
        return {'value': parse_repo(os.environ['GV_REPO'], os.getenv('GV_GIT_HOST', 'gitverse.ru')), 'source': 'GV_REPO'}
    if key == 'http_timeout' and os.getenv('GV_HTTP_TIMEOUT'):
        return {'value': normalize(key, os.environ['GV_HTTP_TIMEOUT'], from_text=True), 'source': 'GV_HTTP_TIMEOUT'}
    return {'value': data['config'].get(key, DEFAULTS[key]), 'source': 'settings' if key in data['config'] else 'default'}


def update(change, dry_run=False):
    path = settings_path()
    if dry_run:
        data = load_settings(); result = change(data); validate(data)
        return {**result, 'path': str(path), 'dry_run': True}
    try: import fcntl
    except ImportError as error: raise GVError('Local settings writes currently require POSIX file locking', 'backend_unsupported') from error
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try: descriptor = os.open(str(path) + '.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), 0o600)
    except OSError as error: raise GVError('Cannot safely open settings lock', 'insecure_settings_file') from error
    temporary = None
    with os.fdopen(descriptor, 'r+b') as lock:
        private_file(os.fstat(lock.fileno()))
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error: raise GVError('Settings are being changed by another process; no write performed', 'settings_busy') from error
        data, snap = read_settings(path)
        result = change(data)
        if not result['changed']: return {**result, 'path': str(path)}
        data = validate(data)
        payload = (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')
        if len(payload) > MAX_BYTES: raise GVError('Settings exceed 1 MiB', 'settings_limit')
        descriptor, temporary = tempfile.mkstemp(prefix='.gv-settings-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as output:
                os.fchmod(output.fileno(), 0o600); output.write(payload); output.flush(); os.fsync(output.fileno())
            if fingerprint(path) != snap: raise GVError('Settings changed outside the lock; reread before retrying', 'concurrent_settings_change')
            os.replace(temporary, path)
        finally:
            if os.path.lexists(temporary): os.unlink(temporary)
    return {**result, 'path': str(path)}


def json_input(filename, timeout):
    import argparse
    import time
    from .actions_config import read_value
    value = read_value(argparse.Namespace(group='alias', value_file=filename, max_bytes=MAX_BYTES), time.monotonic() + timeout)
    return decode(value)


def local_command(args):
    if getattr(args, 'repo', None) or getattr(args, 'backend', None): raise GVError('config/alias are local user settings; -R/--backend are unsupported', 'usage_error')
    path, action = settings_path(), args.action
    dry_run = getattr(args, 'dry_run', False)
    if args.group == 'config':
        if action == 'path': return {'path': str(path), 'exists': os.path.lexists(path), 'auth_file_separate': True}
        if action in ('get', 'list'):
            data = load_settings()
            keys = [args.key] if action == 'get' else list(DEFAULTS)
            result = {}
            for key in keys:
                if key not in DEFAULTS: normalize(key, None)
                result[key] = effective(key, data) if args.effective else {'value': data['config'].get(key, DEFAULTS[key]), 'source': 'settings' if key in data['config'] else 'default'}
            return {'key': args.key, **result[args.key], 'path': str(path)} if action == 'get' else {'config': result, 'path': str(path)}
        value = normalize(args.key, args.value, from_text=True) if action == 'set' else None
        if args.key not in DEFAULTS: normalize(args.key, None)
        def change(data):
            changed = (args.key not in data['config'] or data['config'][args.key] != value) if action == 'set' else args.key in data['config']
            if action == 'set': data['config'][args.key] = value
            else: data['config'].pop(args.key, None)
            return {'key': args.key, 'value': value if action == 'set' else DEFAULTS[args.key], 'changed': changed}
        return update(change, dry_run)
    if action == 'list': return {'aliases': load_settings()['aliases'], 'path': str(path)}
    if action == 'set':
        if (args.expansion is None) == (args.argv_file is None): raise GVError('Supply an expansion string or --argv-file JSON array', 'usage_error')
        incoming = {args.name: alias_tokens(json_input(args.argv_file, args.timeout) if args.argv_file is not None else args.expansion)}
    elif action == 'import':
        incoming = json_input(args.file, args.timeout)
        if isinstance(incoming, dict) and isinstance(incoming.get('aliases'), dict) and set(incoming) <= {'aliases', 'path'}: incoming = incoming['aliases']
        if not isinstance(incoming, dict): raise GVError('Import expects a JSON alias map or alias list output', 'invalid_settings')
        incoming = {name: alias_tokens(value) for name, value in incoming.items()}
    else:
        if args.all == (args.name is not None): raise GVError('Use alias delete NAME or alias delete --all', 'usage_error')
        incoming = {}
    def change(data):
        before = dict(data['aliases'])
        if action == 'delete':
            if args.all: data['aliases'] = {}
            else:
                dependents = [name for name, argv in before.items() if argv[0] == args.name and name != args.name]
                if dependents: raise GVError('Alias is used by other aliases; remove dependents first or use --all', 'alias_in_use', details={'dependents': dependents})
                data['aliases'].pop(args.name, None)
        else:
            if not args.clobber and any(name in before and before[name] != argv for name, argv in incoming.items()): raise GVError('Alias exists with a different expansion; use --clobber explicitly', 'alias_exists')
            data['aliases'].update(incoming)
        validate(data)
        return {'aliases': data['aliases'], 'changed': data['aliases'] != before}
    return update(change, dry_run)


def command_index(argv):
    index = 0
    while index < len(argv) and argv[index].startswith('-'):
        token, _, _ = argv[index].partition('=')
        if token in ('-h', '--help', '--version'): return None
        if token in ('-R', '--repo', '--backend', '--fields'):
            index += 1 if '=' in argv[index] else 2
        elif token in ('--dry-run', '--json') or (argv[index].startswith('-R') and len(argv[index]) > 2): index += 1
        else: return None
    return index if index < len(argv) else None


def bounded(argv):
    if len(argv) > 4096 or sum(len(word.encode('utf-8')) for word in argv) > MAX_BYTES:
        raise GVError('Expanded argv exceeds token/byte bounds', 'alias_limit')
    return argv


def expand_aliases(argv):
    index = command_index(argv)
    if index is None or argv[index] in builtin_names(): return argv
    aliases = load_settings()['aliases']
    if argv[index] not in aliases: return argv
    prefix, current = argv[:index], argv[index:]
    for _ in range(MAX_DEPTH):
        name, supplied = current[0], current[1:]
        template = aliases[name]
        expanded, consumed = [], set()
        has_all = '$@' in template
        for token in template:
            position = POSITION.fullmatch(token)
            if token == '$@': expanded.extend(supplied)
            elif position:
                number = int(position[1]) - 1
                if number >= len(supplied): raise GVError('Alias is missing a positional argument', 'alias_arguments')
                consumed.add(number); expanded.append(supplied[number])
            else: expanded.append(token)
            bounded(prefix + expanded)
        if not has_all: expanded.extend(value for number, value in enumerate(supplied) if number not in consumed)
        current = bounded(prefix + expanded)[len(prefix):]
        if current[0] in builtin_names(): return prefix + current
    raise GVError('Alias expansion depth bound reached', 'alias_limit')
