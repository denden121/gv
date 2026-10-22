"""Internal Git credential helper. Only answers get for a single bound HTTPS repo.

Git invokes this process; never invoke it to inspect credentials in a terminal.
It ignores store/erase, so PAT is not persisted to any Git credential store.
"""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gitverse_cli.client import Client


def answer(operation, request):
    host = os.environ.get('GV_GIT_HELPER_HOST')
    repo = os.environ.get('GV_GIT_HELPER_REPO')
    username = os.environ.get('GV_GIT_HELPER_USERNAME')
    if operation != 'get' or not host or not repo or not username:
        return ''
    if request.get('protocol') != 'https' or request.get('host') != host or request.get('path', '').removesuffix('.git') != repo:
        return ''
    authorization = Client().headers.get('Authorization', '')
    if not authorization.startswith('Bearer '):
        return ''
    token = authorization[len('Bearer '):]
    if any(c in token + username for c in '\r\n'):
        return ''
    return f'username={username}\npassword={token}\n\n'


def main():
    request = {}
    for line in sys.stdin:
        if not line.strip():
            break
        key, sep, value = line.rstrip('\n').partition('=')
        if sep:
            request[key] = value
    try:
        sys.stdout.write(answer(sys.argv[1] if len(sys.argv) > 1 else '', request))
    except Exception:
        # Authentication failure must not print secrets or tracebacks to Git.
        return


if __name__ == '__main__':
    main()
