#!/usr/bin/env python3
"""Read-only synthetic checks for the public reader's light endpoints."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

SITEMAP_NAMESPACE = "http://www.sitemaps.org/schemas/sitemap/0.9"
USER_AGENT = "LociPublicReaderSynthetic/1.0"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_QUERY = "loci synthetic reader check"


class ProbeFailure(Exception):
    def __init__(self, reason: str, http_status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


@dataclass(frozen=True)
class ProbeResult:
    name: str
    status: str
    elapsed_ms: int
    http_status: int | None = None
    failure: str | None = None

    def as_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.http_status is not None:
            result["http_status"] = self.http_status
        if self.failure is not None:
            result["failure"] = self.failure
        return result


def _normalize_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base URL must be an HTTP(S) origin without credentials, path, query, or fragment")
    return f"{parsed.scheme}://{parsed.netloc}"


def _read_response(opener: urllib.request.OpenerDirector, request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.getcode()
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ProbeFailure("redirect", exc.code) from None
        raise ProbeFailure("http_error", exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProbeFailure("network_error") from None

    if status != 200:
        raise ProbeFailure("http_error", status)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ProbeFailure("response_too_large", status)
    return status, body


def _request_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    timeout: float,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    encoded_body = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if encoded_body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=encoded_body, headers=headers, method=method)
    status, raw_body = _read_response(opener, request, timeout)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProbeFailure("invalid_json", status) from None
    return status, payload


def _same_origin(left: urllib.parse.SplitResult, right: urllib.parse.SplitResult) -> bool:
    def port(parsed: urllib.parse.SplitResult) -> int | None:
        try:
            return parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return None

    return (
        left.scheme.lower() == right.scheme.lower()
        and (left.hostname or "").lower() == (right.hostname or "").lower()
        and port(left) == port(right)
    )


def _is_public_path(path: str) -> bool:
    decoded = urllib.parse.unquote(path)
    if decoded in {"/", "/public"}:
        return True
    if "\\" in decoded or any(part in {"", ".", ".."} for part in decoded.split("/")[1:]):
        return False
    prefix = "/evidence/objects/"
    if decoded.startswith(prefix):
        slug = decoded[len(prefix) :]
        return bool(slug) and "/" not in slug
    return False


def _validate_sitemap(raw_body: bytes, base_url: str) -> None:
    try:
        root = ET.fromstring(raw_body)
    except ET.ParseError:
        raise ProbeFailure("invalid_xml", 200) from None

    if root.tag != f"{{{SITEMAP_NAMESPACE}}}urlset":
        raise ProbeFailure("invalid_sitemap", 200)

    base = urllib.parse.urlsplit(base_url)
    loc_count = 0
    for loc in root.findall(f".//{{{SITEMAP_NAMESPACE}}}loc"):
        value = (loc.text or "").strip()
        parsed = urllib.parse.urlsplit(value)
        if not parsed.scheme or not parsed.netloc or not _same_origin(parsed, base):
            raise ProbeFailure("sitemap_origin_mismatch", 200)
        if parsed.query or parsed.fragment or not _is_public_path(parsed.path):
            raise ProbeFailure("sitemap_path_invalid", 200)
        loc_count += 1

    if loc_count == 0:
        raise ProbeFailure("sitemap_empty", 200)


def _run_one(name: str, callback) -> ProbeResult:  # noqa: ANN001
    started = time.monotonic()
    try:
        callback()
    except ProbeFailure as exc:
        return ProbeResult(name, "fail", round((time.monotonic() - started) * 1000), exc.http_status, exc.reason)
    except Exception:
        return ProbeResult(name, "fail", round((time.monotonic() - started) * 1000), failure="probe_error")
    return ProbeResult(name, "ok", round((time.monotonic() - started) * 1000), http_status=200)


def run_checks(base_url: str, *, timeout: float = 10.0, query: str = DEFAULT_QUERY, min_results: int = 0) -> dict[str, Any]:
    base = _normalize_base_url(base_url)
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    if len(query.strip()) < 2:
        raise ValueError("query must contain at least two characters")
    if min_results < 0:
        raise ValueError("minimum result count cannot be negative")

    opener = urllib.request.build_opener(NoRedirectHandler)

    def check_public_collection() -> None:
        _, payload = _request_json(
            opener,
            f"{base}/api/v1/public/objects/page?page=1&page_size=3",
            timeout,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ProbeFailure("invalid_public_response", 200)
        if (
            not isinstance(payload.get("page"), int)
            or payload["page"] < 1
            or payload.get("page_size") != 3
            or not isinstance(payload.get("total"), int)
            or payload["total"] < 0
            or not isinstance(payload.get("total_pages"), int)
            or payload["total_pages"] < 0
        ):
            raise ProbeFailure("invalid_public_response", 200)

    def check_transcript_search() -> None:
        _, payload = _request_json(
            opener,
            f"{base}/api/v1/public/search/segments",
            timeout,
            method="POST",
            body={"query": query, "retrieval_mode": "transcript_only", "page": 1, "page_size": 3},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ProbeFailure("invalid_search_response", 200)
        total = payload.get("total_results")
        if not isinstance(total, int) or total < min_results:
            raise ProbeFailure("insufficient_search_results", 200)

    def check_sitemap() -> None:
        request = urllib.request.Request(
            f"{base}/sitemap.xml",
            headers={"Accept": "application/xml, text/xml;q=0.9", "User-Agent": USER_AGENT},
        )
        _, raw_body = _read_response(opener, request, timeout)
        _validate_sitemap(raw_body, base)

    results = [
        _run_one("public_collection", check_public_collection),
        _run_one("transcript_search", check_transcript_search),
        _run_one("sitemap", check_sitemap),
    ]
    return {
        "status": "ok" if all(item.status == "ok" for item in results) else "fail",
        "checks": [item.as_json() for item in results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run read-only synthetic checks against light public reader endpoints.")
    parser.add_argument("--base-url", required=True, help="Public site origin, for example https://example.org")
    parser.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout in seconds (default: 10)")
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Synthetic transcript query used for the search probe")
    parser.add_argument("--min-results", type=int, default=0, help="Minimum public search result count required")
    args = parser.parse_args(argv)

    try:
        result = run_checks(args.base_url, timeout=args.timeout, query=args.query, min_results=args.min_results)
    except ValueError:
        print(json.dumps({"status": "fail", "checks": [], "failure": "invalid_configuration"}, separators=(",", ":")))
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
