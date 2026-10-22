"""Local attachment planning only; no unverified PR upload/linking requests."""
import hashlib
from pathlib import Path
import time

from .client import GVError
from .comment_attachments import TYPES, TOTAL
from .releases import read_upload


def exists(name):
    try:
        return Path(name).exists()
    except OSError:
        return False


def plan(values, deadline):
    if not 1 <= len(values) <= 50:
        raise GVError('--attach accepts 1..50 files', 'usage_error')
    files, seen, total = [], set(), 0
    for value in values:
        if not isinstance(value, str) or not value or '\0' in value:
            raise GVError('--attach needs a file path without NUL', 'usage_error')
        try:
            if len(value.encode('utf-8')) > 16384:
                raise GVError('Attachment argument exceeds 16 KiB UTF-8', 'usage_error')
        except UnicodeError as error:
            raise GVError('Attachment argument must be valid UTF-8', 'usage_error') from error
        # A literal existing filename wins over the optional #alt suffix.
        positions = [len(value)] + [i for i in reversed(range(len(value))) if value[i] == '#' and i]
        for end in positions:
            if time.monotonic() >= deadline:
                raise GVError('Attachment plan deadline reached', 'deadline_exceeded')
            name = value[:end]
            if exists(name):
                break
        explicit_alt = name != value
        alt = value[len(name) + 1:] if explicit_alt else Path(name).name
        path = Path(name)
        media_type = TYPES.get(path.suffix.lower())
        video = bool(media_type and media_type.startswith('video/'))
        if (name == '-' or not media_type or (video and explicit_alt)
                or any(ord(c) < 32 or ord(c) == 127 or c in '\\"' for c in path.name)):
            raise GVError('Use a supported image/video regular file; video cannot have #alt', 'usage_error')
        try:
            alt.encode('utf-8')
        except UnicodeError as error:
            raise GVError('Image alt must be valid UTF-8', 'usage_error') from error
        if len(alt.encode('utf-8')) > 1048576:
            raise GVError('Image alt exceeds 1 MiB UTF-8', 'usage_error')
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
        files.append({'path': str(path.absolute()), 'name': path.name,
                      'alt': None if video else alt, 'media_type': media_type,
                      'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    return {'status': 'local_plan_only', 'files': files,
            'upload_contract_verified': False, 'final_body_known': False,
            'remote_eligibility_known': False,
            'body_transform': 'Rewrite matching body file references preserving their alt; append remaining successful assets. Not rendered in this plan.',
            'partial_failure': 'Create with successful uploads; return nonzero with PR recovery identity. Not implemented.',
            'limits': {'files': 50, 'image_bytes': 10 * 1024 * 1024,
                       'argument_bytes': 16384,
                       'video_bytes': TOTAL, 'total_bytes': TOTAL,
                       'source': 'local safety policy; not verified GitVerse upload limits'},
            'limitations': 'Filename extensions select media types; contents, rendering, ownership and server permissions are not verified. Live upload/linking remains unimplemented.'}
