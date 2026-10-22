"""Explicit editor input without prompts or contamination of JSON streams."""
import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import time
from types import SimpleNamespace

from .actions_config import read_value
from .client import GVError


def edit_text(initial, deadline, *, max_bytes=1048576):
    try:
        valid = isinstance(initial, str) and '\0' not in initial and len(initial.encode('utf-8')) <= max_bytes
    except UnicodeError:
        valid = False
    if not valid: raise GVError('Invalid or oversized editor seed', 'unexpected_response')
    command = next((os.environ[k] for k in ('GV_EDITOR', 'VISUAL', 'EDITOR')
                    if os.environ.get(k)), 'vi')
    try:
        argv = shlex.split(command)
        if not argv or any('\0' in arg for arg in argv): raise ValueError()
    except ValueError:
        raise GVError('Invalid editor command; configure GV_EDITOR, VISUAL or EDITOR', 'usage_error') from None
    remaining = deadline - time.monotonic()
    if remaining <= 0: raise GVError('Editor deadline reached', 'deadline_exceeded')
    # The editor needs the draft, not API credentials. No shell, no captured
    # output buffers, no raw subprocess exceptions in JSON errors.
    env = {k: v for k, v in os.environ.items() if k not in
           ('GITVERSE_TOKEN', 'GV_TOKEN', 'GITVERSE_COOKIE_FILE', 'GITVERSE_CSRF_TOKEN')}
    with tempfile.TemporaryDirectory(prefix='gv-comment-') as directory:
        draft = Path(directory) / 'comment.md'
        draft.write_bytes(initial.encode('utf-8'))
        draft.chmod(0o600)
        try:
            terminal = open('/dev/tty', 'r+b', buffering=0)
        except OSError:
            terminal = open(os.devnull, 'r+b', buffering=0)
        with terminal:
            try:
                process = subprocess.Popen([*argv, str(draft)], stdin=terminal, stdout=terminal,
                                           stderr=terminal, env=env, start_new_session=True)
            except (OSError, ValueError):
                raise GVError('Could not start editor; no content submitted', 'editor_error') from None
            try:
                status = process.wait(timeout=max(0, deadline - time.monotonic()))
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait()
                raise GVError('Editor interrupted or deadline reached; no content submitted', 'deadline_exceeded') from None
        if status:
            raise GVError('Editor exited unsuccessfully; no content submitted', 'editor_error')
        text = read_value(SimpleNamespace(value_file=str(draft), max_bytes=max_bytes, group='pr'), deadline)
        if not text.strip(): raise GVError('Empty editor draft; no content submitted', 'usage_error')
        return text
