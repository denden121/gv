"""Bounded public comment attachments; returned asset URLs are never requested."""
import hashlib
import os
from pathlib import Path
import re
import secrets
from urllib.parse import unquote, urlsplit
from uuid import UUID

from .client import GVError
from .releases import read_upload

TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
         '.gif': 'image/gif', '.webp': 'image/webp', '.svg': 'image/svg+xml',
         '.mp4': 'video/mp4', '.mov': 'video/quicktime', '.webm': 'video/webm'}
TOTAL = 100 * 1024 * 1024


def prepare(values, deadline):
    if not 1 <= len(values) <= 50:
        raise GVError('--attach accepts 1..50 files', 'usage_error')
    assets, seen, total = [], set(), 0
    for value in values:
        if not value or '\0' in value:
            raise GVError('--attach needs a file path without NUL', 'usage_error')
        # Existing filenames containing hashes win; otherwise longest existing prefix.
        candidates = [value] + [value[:i] for i in reversed(range(len(value))) if value[i] == '#' and i]
        name = next((p for p in candidates if Path(p).exists()), candidates[-1])
        alt = value[len(name) + 1:] if name != value else ''
        path = Path(name)
        content_type = TYPES.get(path.suffix.lower())
        video = bool(content_type and content_type.startswith('video/'))
        if name == '-' or not content_type or (video and alt) or any(ord(c) < 32 or c in '\\"' for c in path.name):
            raise GVError('Attach a supported image/video regular file; video cannot have alt text; filename cannot contain controls, quotes or backslashes', 'usage_error')
        maximum = TOTAL if video else 10 * 1024 * 1024
        try:
            info = path.stat()
            key = (info.st_dev, info.st_ino)
            if key in seen:
                raise GVError('Attached files must be unique', 'usage_error')
            if total + info.st_size > TOTAL:
                raise GVError('Total attachment bytes exceed 100 MiB', 'upload_limit')
            data = read_upload(path, maximum, deadline)
        except OSError as error:
            raise GVError('Cannot read attachment regular file', 'usage_error') from error
        if not data:
            raise GVError('Attachment is empty', 'usage_error')
        total += len(data)
        if total > TOTAL:
            raise GVError('Total attachment bytes exceed 100 MiB', 'upload_limit')
        seen.add(key)
        assets.append({'path': str(path.absolute()), 'name': path.name, 'alt': alt or path.stem.replace('.', ' '),
                       'video': video, 'type': content_type, 'data': data,
                       'sha256': hashlib.sha256(data).hexdigest()})
    return assets


def asset_value(row):
    if (not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0
            or type(row.get('size')) is not int or row['size'] < 0
            or not isinstance(row.get('name'), str) or not row['name']
            or not isinstance(row.get('uuid'), str) or not isinstance(row.get('browser_download_url'), str)):
        raise GVError('Invalid comment attachment metadata', 'unexpected_response')
    try:
        if str(UUID(row['uuid'])) != row['uuid']: raise ValueError()
        url = row['browser_download_url']
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.netloc != os.environ.get('GV_GIT_HOST', 'gitverse.ru')
                or parsed.path != '/api/attachments/' + row['uuid']
                or parsed.fragment or parsed.query or any(c.isspace() or c in '<>()[]\\' for c in url)):
            raise ValueError()
    except ValueError as error:
        raise GVError('Unsafe or invalid comment attachment URL/UUID', 'unexpected_response') from error
    return {k: row[k] for k in ('id', 'uuid', 'name', 'size', 'browser_download_url')}


def attachment_list(row):
    rows = row.get('attachments')
    if not isinstance(rows, list) or len(rows) > 50:
        raise GVError('Missing or oversized comment attachment list', 'unexpected_response')
    result = [asset_value(r) for r in rows]
    if len({r['id'] for r in result}) != len(result) or len({r['uuid'] for r in result}) != len(result):
        raise GVError('Duplicate comment attachment identities', 'unexpected_response')
    return result


def multipart(asset):
    boundary = 'gv-' + secrets.token_hex(24)
    wire = (f'--{boundary}\r\nContent-Disposition: form-data; name="attachment"; filename="{asset["name"]}"\r\n'
            f'Content-Type: {asset["type"]}\r\n\r\n').encode('utf-8') + asset['data'] + f'\r\n--{boundary}--\r\n'.encode()
    return wire, 'multipart/form-data; boundary=' + boundary


def escape_alt(value):
    return value.replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]').replace('\r', ' ').replace('\n', ' ')


def render(text, assets, urls, body_file=None):
    """Rewrite source ranges for inline and used reference destinations outside code.

    A deliberately bounded scanner leaves complex Markdown unchanged; assets
    with no safe replacement are appended, so no upload disappears.
    """
    directories = [Path(body_file).absolute().parent] if body_file and body_file != '-' else []
    directories.append(Path.cwd())
    used = set()
    def match(destination):
        raw = destination[1:-1] if destination.startswith('<') else destination
        if raw.startswith(('#', '//')): return None
        try:
            if urlsplit(raw).scheme: return None
        except ValueError:
            return None
        unescaped = re.sub(r'\\([!"#$%&\'()*+,./:;<=>?@\[\]\\^_`{|}~-])', r'\1', raw)
        spellings = list(dict.fromkeys((raw, unescaped, unquote(raw), unquote(unescaped))))
        for directory in directories:
            for spelling in spellings:
                target = os.path.abspath(directory / spelling)
                for i, asset in enumerate(assets):
                    if target == asset['path']: return i
        return None
    from .attachment_markdown import references, escaped
    inline, definitions = references(text)
    edits = []
    for ref in inline:
        begin, end, destination, image, label, node_start, node_end, standalone = ref
        i = match(destination)
        if i is None:
            continue
        used.add(i)
        if assets[i]['video'] and image:
            if standalone:
                edits.append((node_start, node_end, urls[i]))
            else:
                # Preserve nested label formatting and titles. Escaping a
                # preceding bang prevents the degraded link becoming an image.
                bang_start = node_start
                replacement = ''
                if bang_start and text[bang_start - 1] == '!' and not escaped(text, bang_start - 1):
                    bang_start -= 1
                    replacement = '\\!'
                edits.append((bang_start, node_start + 1, replacement))
                if not label:
                    edits.append((node_start + 2, node_start + 2, escape_alt(assets[i]['alt'])))
                edits.append((begin, end, urls[i]))
        else:
            edits.append((begin, end, urls[i]))
    for begin, end, destination, image in definitions:
        i = match(destination)
        if i is None:
            continue
        if image and assets[i]['video']:
            raise GVError('Cannot embed a video as a reference-style image; use an inline image or a link', 'usage_error')
        used.add(i)
        edits.append((begin, end, urls[i]))
    # Duplicate uses share one definition edit. Applying source ranges avoids
    # normalizing body alt text, titles, indentation or container markers.
    pieces, previous = [], len(text)
    for begin, end, replacement in sorted(set(edits), reverse=True):
        if end > previous:
            continue
        pieces.extend((text[end:previous], replacement))
        previous = begin
    pieces.append(text[:previous])
    result = ''.join(reversed(pieces))
    for i, asset in enumerate(assets):
        if i not in used:
            addition = urls[i] if asset['video'] else '![' + escape_alt(asset['alt']) + '](' + urls[i] + ')'
            result = result.rstrip(' \t\r\n')
            result += ('\n\n' if result else '') + addition
    if len(result.encode('utf-8')) > 1048576:
        raise GVError('Rendered comment exceeds 1 MiB', 'usage_error')
    return result
