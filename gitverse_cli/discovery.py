"""Offline schema discovery and HAR inventory. Never retain traffic payloads."""
import collections
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .client import GVError


def spec():
    return json.loads((Path(__file__).parent / "data" / "openapi.json").read_text())


def endpoints(search="", backend="public"):
    if backend == "web":
        records = json.loads((Path(__file__).parent / "data" / "web-endpoints.json").read_text())["endpoints"]
        return [r for r in records if search.lower() in r["path"].lower()]
    s = spec()
    return [{"method": m.upper(), "path": p, "summary": v.get("summary", ""), "backend": "public"}
            for p, methods in s["paths"].items() for m, v in methods.items()
            if m in ("get", "post", "put", "patch", "delete", "head")
            and search.lower() in (p + " " + v.get("summary", "")).lower()]


def schema(path, method="GET"):
    s = spec()
    operation = s["paths"].get(path, {}).get(method.lower())
    if operation is None:
        raise GVError("Operation not found; use `gv api endpoints --search pulls`", "unknown_operation")
    refs = {}

    def collect(v):
        if isinstance(v, dict):
            ref = v.get("$ref", "")
            if ref.startswith("#/definitions/") and ref not in refs:
                refs[ref] = s["definitions"][ref.split("/")[-1]]
                collect(refs[ref])
            for item in v.values():
                collect(item)
        elif isinstance(v, list):
            for item in v:
                collect(item)

    collect(operation)
    return {"method": method.upper(), "path": path, "operation": operation, "definitions": refs}


def traffic_inventory(files):
    """Keep method/path and field names, never header/query/body values."""
    groups = {}
    total = 0
    for name in files:
        content = json.loads(Path(name).read_text())
        for e in content["log"]["entries"]:
            req = e["request"]
            url = urlsplit(req["url"])
            if url.hostname not in ("gitverse.ru", "api.gitverse.ru"):
                continue
            path = url.path
            backend = "public" if url.hostname == "api.gitverse.ru" else "web" if path.startswith("/sbt/api/v1/") else "site"
            if backend == "site":
                continue
            if backend == "web":
                path = path.removeprefix("/sbt/api/v1")
            # Remove account/repository names and IDs from the saved inventory.
            path = re.sub(r"^/repos/[^/]+/[^/]+(?=/|$)", "/repos/{owner}/{repo}", path)
            path = re.sub(r"^/users/[^/]+(?=/|$)", "/users/{user}", path)
            path = re.sub(r"(?<=/)\d+(?=/|$)", "{id}", path)
            key = (backend, req["method"], path)
            item = groups.setdefault(key, {"backend": backend, "method": req["method"], "path": path, "observations": 0, "statuses": collections.Counter(), "query_fields": set(), "request_fields": set(), "response_fields": set()})
            item["observations"] += 1
            total += 1
            item["statuses"][str(e["response"]["status"])] += 1
            item["query_fields"].update(parse_qs(url.query))
            for section, raw in (("request_fields", req.get("postData", {}).get("text", "")), ("response_fields", e["response"].get("content", {}).get("text", ""))):
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        item[section].update(obj)
                except (ValueError, TypeError):
                    pass
    out = []
    for key in sorted(groups):
        item = groups[key]
        for field in ("query_fields", "request_fields", "response_fields"):
            item[field] = sorted(item[field])
        item["statuses"] = dict(item["statuses"])
        out.append(item)
    return {"requests": total, "endpoints": out, "contains_payloads": False}
