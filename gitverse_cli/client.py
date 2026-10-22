"""Dependency-free HTTP transport; credentials never cross origins or backends."""
import http.client
import http.cookiejar
import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from . import __version__


class GVError(Exception):
    def __init__(self, message, code="request_failed", status=None, details=None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details

    def as_dict(self):
        result = {"code": self.code, "message": str(self)}
        if self.status is not None:
            result["status"] = self.status
        if self.details is not None:
            result["details"] = self.details
        return {"error": result}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_base(value):
    p = urllib.parse.urlsplit(value)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise GVError("API base must be an HTTPS URL without credentials, query or fragment", "invalid_url")
    return value.rstrip("/")


def safe_url(base, path):
    p = urllib.parse.urlsplit(path)
    decoded = urllib.parse.unquote(p.path)
    if p.scheme or p.netloc or p.fragment or "\\" in decoded or any(x in ("..", ".") for x in decoded.split("/")):
        raise GVError("Use a relative API path without traversal or fragments", "invalid_path")
    return base.rstrip("/") + "/" + path.lstrip("/")


def config_path():
    return Path(os.environ.get("GV_CONFIG", str(Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "gitverse-agent-cli" / "auth.json")))


def load_auth():
    p = config_path()
    if not p.exists():
        return {}
    if p.stat().st_mode & 0o077:
        raise GVError("Auth file must have permissions 0600", "insecure_auth_file")
    return json.loads(p.read_text())


class Client:
    def __init__(self, backend="public", dry_run=False, timeout=None, public_base=None, web_base=None):
        if timeout is None:
            from .settings import effective
            timeout = effective('http_timeout')['value']
        self.backend, self.dry_run, self.timeout = backend, dry_run, timeout
        self.base = validate_base(
            public_base or os.environ.get("GV_API_URL", "https://api.gitverse.ru")
            if backend == "public" else web_base or os.environ.get("GV_WEB_API_URL", "https://gitverse.ru/sbt/api/v1")
        )
        self.headers = {"User-Agent": "gitverse-agent-cli/" + __version__, "Accept": "application/vnd.gitverse.object+json; version=1" if backend == "public" else "application/json"}
        handlers = [NoRedirect()]
        self.auth_source = None
        if not dry_run:
            if backend == "public":
                token = os.environ.get("GITVERSE_TOKEN") or os.environ.get("GV_TOKEN")
                if token:
                    self.auth_source = "environment"
                else:
                    saved = load_auth()
                    # A saved token is bound to the API URL used when logging in.
                    token = saved.get("token") if saved.get("api_url") == self.base else None
                    if token:
                        self.auth_source = "config"
                if token:
                    self.headers["Authorization"] = "Bearer " + token.strip()
            else:
                cookie_file = os.environ.get("GITVERSE_COOKIE_FILE")
                if cookie_file:
                    jar = http.cookiejar.MozillaCookieJar(cookie_file)
                    jar.load(ignore_discard=True, ignore_expires=False)
                    handlers.append(urllib.request.HTTPCookieProcessor(jar))
                    self.auth_source = "cookie_file"
                csrf = os.environ.get("GITVERSE_CSRF_TOKEN")
                if csrf:
                    self.headers["X-CSRF-Token"] = csrf
        self.opener = urllib.request.build_opener(*handlers)

    def request(self, method, path, query=None, body=None, raw=False, deadline=None, require_complete=False, max_response_bytes=None, encoded_body=None, content_type=None, expected_status=None):
        url = safe_url(self.base, path)
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query, doseq=True)
        method = method.upper()
        if encoded_body is not None and (body is not None or not isinstance(encoded_body, bytes) or self.backend != 'public' or method != 'POST' or not isinstance(content_type, str) or '\r' in content_type or '\n' in content_type):
            raise GVError('Encoded uploads require a public POST, bytes and an explicit content type', 'usage_error')
        if self.dry_run:
            return {"method": method, "url": url, "body": {'encoded_bytes': len(encoded_body)} if encoded_body is not None else body, "backend": self.backend}
        data = encoded_body if encoded_body is not None else json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = content_type if encoded_body is not None else "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        for attempt in range(3):
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise GVError('Request deadline reached', 'deadline_exceeded')
            try:
                with self.opener.open(req, timeout=self.timeout if remaining is None else min(self.timeout, remaining)) as response:
                    if expected_status is not None and response.status != expected_status:
                        raise GVError('Unexpected HTTP success status', 'unexpected_response', response.status)
                    if require_complete and response.status == 206:
                        raise GVError('Server returned partial content; no truncated result is returned', 'incomplete_response', 206)
                    # HTTPResponse.read1() can return EOF without raising
                    # IncompleteRead, leaving a nonzero framing length. Capture
                    # the parser's original length (None for chunked/EOF framing)
                    # before reading; valid JSON alone cannot prove completeness.
                    declared_length = getattr(response, 'length', None)
                    if max_response_bytes is None:
                        payload = response.read()
                    else:
                        chunks, size = [], 0
                        while True:
                            if deadline is not None and time.monotonic() >= deadline:
                                raise GVError('Request deadline reached', 'deadline_exceeded')
                            chunk = response.read1(min(65536, max_response_bytes - size + 1))
                            if not chunk: break
                            size += len(chunk)
                            if size > max_response_bytes: raise GVError('API response byte limit exceeded', 'response_limit')
                            chunks.append(chunk)
                        payload = b''.join(chunks)
                    if require_complete and type(declared_length) is int and len(payload) != declared_length:
                        raise GVError('HTTP response ended before its declared length', 'incomplete_response', response.status)
                    if not payload:
                        return None
                    try: text = payload.decode('utf-8', errors='strict' if require_complete else 'replace')
                    except UnicodeDecodeError as error:
                        raise GVError('Expected UTF-8 JSON response', 'unexpected_response') from error
                    if raw:
                        return text
                    try:
                        return json.loads(text)
                    except ValueError:
                        raise GVError("Expected JSON; server returned another format (possibly a login page)", "unexpected_response", response.status)
            except urllib.error.HTTPError as e:
                if method in ("GET", "HEAD") and e.code in (429, 502, 503, 504) and attempt < 2:
                    delay = e.headers.get("Retry-After", "")
                    e.close()
                    wait = min(float(delay), 5) if delay.isdigit() else 0.5 * 2**attempt
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise GVError('Request deadline reached', 'deadline_exceeded')
                        wait = min(wait, remaining)
                    time.sleep(wait)
                    continue
                # Do not print arbitrary error bodies: they may contain cookies/tokens.
                e.close()
                hint = {401: "Authentication required: set GITVERSE_TOKEN (public) or GITVERSE_COOKIE_FILE (web)", 403: "Access forbidden: check token scopes and repository permissions", 404: "Resource not found or not accessible", 422: "Request validation failed; inspect `gv api schema`", 429: "Rate limit exceeded"}.get(e.code, "HTTP request failed")
                raise GVError(hint, "http_error", e.code)
            except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
                # Mutation outcome may be unknown. Never retry a POST/PATCH automatically.
                if deadline is not None and time.monotonic() >= deadline:
                    raise GVError('Request deadline reached', 'deadline_exceeded') from e
                raise GVError("Network error; mutation outcome may be unknown" if method not in ("GET", "HEAD") else "Network error: check DNS, proxy and connectivity", "network_error") from e

    def download_to(self, path, output, max_bytes, deadline):
        """Stream a same-API binary GET. Never follow redirects or retry streams."""
        if self.backend != 'public' or self.dry_run:
            raise GVError('Binary downloads require the public API outside dry-run', 'backend_unsupported')
        # GitVerse requires the versioned API Accept even for a binary ZIP body.
        headers = dict(self.headers)
        request = urllib.request.Request(safe_url(self.base, path), headers=headers, method='GET')
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise GVError('Download deadline reached', 'deadline_exceeded')
        try:
            with self.opener.open(request, timeout=min(self.timeout, remaining)) as response:
                if getattr(response, 'status', 200) != 200:
                    raise GVError('Expected a complete binary response', 'incomplete_download')
                length = response.headers.get('Content-Length')
                if length is not None:
                    if not length.isdecimal(): raise GVError('Invalid download length', 'unexpected_response')
                    if int(length) > max_bytes: raise GVError('Compressed download size limit exceeded', 'download_limit')
                size, digest = 0, hashlib.sha256()
                while True:
                    if time.monotonic() >= deadline: raise GVError('Download deadline reached', 'deadline_exceeded')
                    chunk = response.read1(65536)
                    if not chunk: break
                    size += len(chunk)
                    if size > max_bytes: raise GVError('Compressed download size limit exceeded', 'download_limit')
                    output.write(chunk); digest.update(chunk)
                if length is not None and size != int(length):
                    raise GVError('Truncated binary download', 'incomplete_download')
                return {'bytes': size, 'sha256': digest.hexdigest()}
        except urllib.error.HTTPError as error:
            status = error.code; error.close()
            raise GVError('Binary download failed; redirects are not followed', 'http_error', status) from error
        except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.HTTPException) as error:
            raise GVError('Binary download interrupted; no automatic retry', 'network_error') from error

    def pages(self, path, query=None, paginate=False, page_size=30, page_key="per_page", max_pages=100):
        q = dict(query or {})
        q.setdefault(page_key, page_size)
        q.setdefault("page", 1)
        if self.dry_run or not paginate:
            return self.request("GET", path, q)
        combined = []
        for _ in range(max_pages):
            result = self.request("GET", path, q)
            items = result if isinstance(result, list) else next((result[k] for k in ("items", "data", "workflow_runs", "jobs", "runs", "reviews", "comments") if isinstance(result, dict) and isinstance(result.get(k), list)), None)
            if items is None:
                raise GVError("Unknown pagination envelope; use `gv api` to inspect it", "pagination_error")
            combined.extend(items)
            if len(items) < q[page_key]:
                return combined
            q["page"] += 1
        raise GVError("Pagination bound reached; narrow the query or request explicit pages", "pagination_limit", details={"max_pages": max_pages})
