"""Repository filename catalog over verified public Git objects (no web API)."""
from types import SimpleNamespace

from .client import GVError
from .contents import get, read_blob, resolve_ref, tree
from .repositories import identity

LIMITS = SimpleNamespace(max_pages=100, page_size=100, max_entries=100000)


def catalog_name(value):
    try:
        valid = (isinstance(value, str) and bool(value) and value not in ('.', '..', '-')
                 and len(value.encode('utf-8')) <= 1024
                 and not any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in '/\\' for c in value))
    except UnicodeError:
        valid = False
    if not valid:
        raise GVError('Template catalog name must be a filename without controls or traversal', 'usage_error')
    return value


def resolve(client, repo, name, deadline):
    """Pin default branch once; absence requires a complete tree, never a 404."""
    name = catalog_name(name)
    repository = identity(get(client, f'repos/{repo}', None, deadline), repo)
    default = repository.get('default_branch')
    if not isinstance(default, str) or not default:
        raise GVError('Template repository default branch is missing or malformed', 'unexpected_response')
    ref, commit = resolve_ref(client, repo, default, deadline, LIMITS)
    rows = tree(client, repo, commit, LIMITS, deadline)
    candidates = []
    for parent in ('.github', '', 'docs'):
        directories = [r['path'] for r in rows if r['type'] == 'dir'
                       and r['path'].rpartition('/')[0] == parent
                       and r['name'].casefold() == 'pull_request_template']
        if len(directories) > 1:
            raise GVError('Ambiguous template catalog directories', 'ambiguous_template')
        if not directories:
            continue
        candidates = [r for r in rows if r['type'] == 'file'
                      and r['path'].rpartition('/')[0] == directories[0]
                      and r['name'].endswith('.md')]
        if candidates:
            break
    selected = next((r for r in candidates if r['name'] == name), None)
    if selected is None:
        raise GVError('Template name not found in the complete pinned repository catalog', 'template_not_found')
    binary = read_blob(client, repo, commit, selected, 1048576, deadline)
    try:
        body = binary.decode('utf-8')
    except UnicodeError:
        raise GVError('Template must be UTF-8', 'usage_error') from None
    return body, {'repository_id': repository['id'], 'commit_sha': commit,
                  'path': selected['path'], 'blob_sha': selected['sha'], 'ref': ref}
