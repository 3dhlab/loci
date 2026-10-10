from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).with_name("check-public-reader.py")
SPEC = importlib.util.spec_from_file_location("check_public_reader", SCRIPT_PATH)
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = checker
SPEC.loader.exec_module(checker)


class FakeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.search_request: dict[str, object] | None = None
        self.user_agents: list[str] = []
        super().__init__(("127.0.0.1", 0), FakeHandler)


class FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.server.user_agents.append(self.headers.get("User-Agent", ""))
        if self.path.startswith("/api/v1/public/objects/page"):
            # Representative PublicObjectPageResponse contract from the API schema.
            self._send(200, b'{"items":[],"page":1,"page_size":3,"total":0,"total_pages":0}')
            return
        if self.path == "/sitemap.xml":
            mode = self.server.mode
            if mode == "redirect":
                self.send_response(302)
                self.send_header("Location", "/redirect-target")
                self.end_headers()
                return
            if mode == "malformed_xml":
                self._send(200, b"<urlset>", "application/xml")
                return
            if mode == "mixed_origin_sitemap":
                body = b"<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\"><url><loc>https://elsewhere.invalid/public</loc></url></urlset>"
                self._send(200, body, "application/xml")
                return
            origin = f"http://{self.headers['Host']}"
            body = (
                f'<urlset xmlns="{checker.SITEMAP_NAMESPACE}">'
                f"<url><loc>{origin}/</loc></url>"
                f"<url><loc>{origin}/public</loc></url>"
                f"<url><loc>{origin}/evidence/objects/synthetic-check</loc></url>"
                "</urlset>"
            ).encode()
            self._send(200, body, "application/xml")
            return
        self._send(404, b"{}")

    def do_POST(self) -> None:
        self.server.user_agents.append(self.headers.get("User-Agent", ""))
        length = int(self.headers.get("Content-Length", "0"))
        self.server.search_request = json.loads(self.rfile.read(length))
        mode = self.server.mode
        if mode == "search_500":
            self._send(500, b"{}")
        elif mode == "malformed_json":
            self._send(200, b"not json")
        else:
            self._send(200, b'{"results":[],"total_results":0}')


class CheckPublicReaderTests(unittest.TestCase):
    def run_server(self, mode: str, *, min_results: int = 0) -> tuple[dict[str, object], FakeServer]:
        server = FakeServer(mode)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = checker.run_checks(f"http://127.0.0.1:{server.server_port}", timeout=2, query="private-synthetic-query", min_results=min_results)
            return result, server
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_success_checks_public_collection_search_and_public_sitemap(self) -> None:
        result, server = self.run_server("ok")

        self.assertEqual(result["status"], "ok")
        self.assertEqual([check["name"] for check in result["checks"]], ["public_collection", "transcript_search", "sitemap"])
        self.assertEqual(server.search_request["retrieval_mode"], "transcript_only")
        self.assertEqual(server.search_request["query"], "private-synthetic-query")
        self.assertTrue(all(agent == checker.USER_AGENT for agent in server.user_agents))
        rendered = json.dumps(result)
        self.assertNotIn("private-synthetic-query", rendered)
        self.assertNotIn("synthetic-check", rendered)

    def test_search_http_500_fails_the_check(self) -> None:
        result, _ = self.run_server("search_500")

        self.assertEqual(result["status"], "fail")
        search = next(check for check in result["checks"] if check["name"] == "transcript_search")
        self.assertEqual(search["failure"], "http_error")
        self.assertEqual(search["http_status"], 500)

    def test_malformed_search_json_fails_the_check(self) -> None:
        result, _ = self.run_server("malformed_json")

        search = next(check for check in result["checks"] if check["name"] == "transcript_search")
        self.assertEqual(search["failure"], "invalid_json")

    def test_malformed_sitemap_xml_fails_the_check(self) -> None:
        result, _ = self.run_server("malformed_xml")

        sitemap = next(check for check in result["checks"] if check["name"] == "sitemap")
        self.assertEqual(sitemap["failure"], "invalid_xml")

    def test_mixed_origin_sitemap_url_fails_the_check(self) -> None:
        result, _ = self.run_server("mixed_origin_sitemap")

        sitemap = next(check for check in result["checks"] if check["name"] == "sitemap")
        self.assertEqual(sitemap["failure"], "sitemap_origin_mismatch")

    def test_redirect_is_reported_without_following(self) -> None:
        result, _ = self.run_server("redirect")

        sitemap = next(check for check in result["checks"] if check["name"] == "sitemap")
        self.assertEqual(sitemap["failure"], "redirect")
        self.assertEqual(sitemap["http_status"], 302)

    def test_cli_returns_nonzero_when_any_probe_fails(self) -> None:
        output = io.StringIO()
        with patch.object(checker, "run_checks", return_value={"status": "fail", "checks": []}), redirect_stdout(output):
            exit_code = checker.main(["--base-url", "https://example.org"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "fail")


if __name__ == "__main__":
    unittest.main()
