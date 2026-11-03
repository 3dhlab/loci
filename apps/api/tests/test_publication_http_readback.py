"""Actual loopback HTTP client boundaries with generated responses and bytes."""
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import sys
from threading import Thread
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.publication_http_readback import HTTPReadbackError, public_base, verify_http


@contextmanager
def generated_http(routes):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            status, body, headers = routes.get(self.path, (404, b"", {}))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Generated oversize responses deliberately outlive a client
                # which has already enforced its bound and closed the stream.
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class HTTPReadbackTests(unittest.TestCase):
    def setUp(self):
        self.media = b"generated model bytes"
        self.video = b"generated video bytes"
        self.manifest = {"objects": [{"id": "object", "website_object_id": "stable-object", "is_embed_ready": True}],
                         "object_models": [{"object_id": "object", "public_media_relpath": "model.glb"}],
                         "videos": [{"id": "video", "object_id": "object", "public_media_relpath": "video.mp4",
                                     "stable_video_id": "generated-video", "publication_attribution_json": []}],
                         "object_model_annotations": [{"id": "annotation", "object_id": "object", "video_id": "video",
                             "website_annotation_id": "stable-annotation", "title": "Generated observation", "description": None,
                             "start_ms": 0, "end_ms": 1000, "review_status": "APPROVED", "is_published": True}]}
        self.inventory = {n: {"size_bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()}
                          for n, b in (("model.glb", self.media), ("video.mp4", self.video))}
        annotation = self.manifest["object_model_annotations"][0]
        preview = {"object": {"id": "object"}, "annotations": [annotation]}
        evidence = {"object": {"id": "stable-object"}, "canonical_url": "/evidence/objects/stable-object?annotation=stable-annotation",
                    "focus": {"video_id": "generated-video", "start_ms": 0},
                    "playback": {"video_id": "generated-video"},
                    "citation_attribution": None, "citation_attribution_timeline": [],
                    "selected_annotation": {"id": "stable-annotation", "title": annotation["title"],
                                            "start_ms": 0, "end_ms": 1000}}
        json_response = lambda value: (200, json.dumps(value).encode(), {"Content-Type": "application/json"})
        self.routes = {"/api/v1/public/objects": json_response([{"id": "object"}]),
                       "/api/v1/public/objects/object/preview": json_response(preview),
                       "/api/v1/public/objects/object/model/file": (200, self.media, {}),
                       "/api/v1/public/videos/video/stream": (200, self.video, {}),
                       "/api/v1/public/evidence/objects/stable-object": json_response(evidence),
                       "/api/v1/public/evidence/objects/stable-object?annotation_id=stable-annotation": json_response(evidence)}

    def test_real_http_checks_bytes_annotations_and_stable_citation_without_credentials(self):
        with generated_http(self.routes) as (base, requests):
            receipt = verify_http(self.manifest, self.inventory, base)
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["resource_count"], 6)
        for _, headers in requests:
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("Cookie", headers)
            self.assertEqual(headers["Accept-Encoding"], "identity")

    def test_changed_short_long_and_missing_media_fail(self):
        path = "/api/v1/public/objects/object/model/file"
        for body, code in ((b"x" * len(self.media), 200), (self.media[:-1], 200),
                           (self.media + b"x", 200), (b"", 404)):
            with self.subTest(code=code, body=body):
                routes = {**self.routes, path: (code, body, {})}
                with generated_http(routes) as (base, _), self.assertRaises(HTTPReadbackError):
                    verify_http(self.manifest, self.inventory, base)

    def test_redirect_is_never_followed(self):
        routes = {**self.routes, "/api/v1/public/objects/object/preview":
                  (302, b"", {"Location": "https://example.invalid/private"})}
        with generated_http(routes) as (base, requests), self.assertRaises(HTTPReadbackError):
            verify_http(self.manifest, self.inventory, base)
        self.assertEqual(len(requests), 2)

    def test_clip_bytes_poster_and_stable_focus_checked(self):
        self.manifest["clips"] = [{"id": "clip", "video_id": "video", "website_clip_id": "stable-clip",
            "public_media_relpath": "clip.mp4", "public_poster_relpath": "poster.png",
            "start_ms": 0, "end_ms": 500, "transcript_excerpt": "Generated excerpt",
            "citation_text": "Synthetic clip citation"}]
        self.manifest["public_clip_excerpts"] = {"clip": "Generated excerpt"}
        for name, data in (("clip.mp4", b"clip"), ("poster.png", b"poster")):
            self.inventory[name] = {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        self.routes["/api/public/clips/stable-clip/stream"] = (200, b"clip", {})
        self.routes["/api/public/clips/stable-clip/poster"] = (200, b"poster", {})
        self.routes["/api/public/objects/stable-object"] = (200,
            json.dumps({"clips": [{"id": "stable-clip", "description": "Synthetic clip citation"}]}).encode(), {})
        self.routes["/api/v1/public/evidence/objects/stable-object?clip_id=stable-clip"] = (200,
            json.dumps({"canonical_url": "/evidence/objects/stable-object?clip=stable-clip",
                        "focus": {"video_id": "generated-video", "start_ms": 0},
                        "playback": {"video_id": "generated-video"},
                        "citation_attribution": None, "citation_attribution_timeline": [],
                        "selected_clip": {"id": "stable-clip", "start_ms": 0, "end_ms": 500,
                                         "transcript_excerpt": "Generated excerpt"}}).encode(), {})
        with generated_http(self.routes) as (base, _):
            result = verify_http(self.manifest, self.inventory, base)
        self.assertEqual(result["resource_count"], 10)
        self.manifest["clips"][0]["transcript_excerpt"] = None
        self.manifest["public_clip_excerpts"] = {"clip": "Selected metadata fallback"}
        focused_path = "/api/v1/public/evidence/objects/stable-object?clip_id=stable-clip"
        evidence = json.loads(self.routes[focused_path][1])
        evidence["selected_clip"]["transcript_excerpt"] = "Selected metadata fallback"
        self.routes[focused_path] = (200, json.dumps(evidence).encode(), {})
        with generated_http(self.routes) as (base, _): verify_http(self.manifest, self.inventory, base)
        self.routes["/api/public/objects/stable-object"] = (200,
            json.dumps({"clips": [{"id": "stable-clip", "description": "Wrong citation"}]}).encode(), {})
        with generated_http(self.routes) as (base, _), self.assertRaisesRegex(HTTPReadbackError, "citation text"):
            verify_http(self.manifest, self.inventory, base)

    def test_playlist_and_annotation_bindings_are_checked(self):
        expected = self.manifest["object_model_annotations"][0]
        expected.update(playlist_json=[{"video_id": "video", "clip_id": None,
            "transcript_segment_id": None, "label": "Selected clip", "start_ms": 0, "end_ms": 1000}],
            model_revision_created_against=3, transcript_segment_id=None, clip_id=None)
        path = "/api/v1/public/objects/object/preview"
        public = {**expected, "playlist": expected["playlist_json"]}
        for changes, valid in (({}, True), ({"playlist": []}, False),
                               ({"model_revision_created_against": 4}, False),
                               ({"transcript_segment_id": "different"}, False)):
            routes = {**self.routes, path: (200, json.dumps({"object": {"id": "object"},
                       "annotations": [{**public, **changes}]}).encode(), {})}
            with self.subTest(changes=changes), generated_http(routes) as (base, _):
                if valid: verify_http(self.manifest, self.inventory, base)
                else:
                    with self.assertRaises(HTTPReadbackError): verify_http(self.manifest, self.inventory, base)

    def test_citation_focus_query_must_resolve_selected_annotation(self):
        path = "/api/v1/public/evidence/objects/stable-object?annotation_id=stable-annotation"
        payload = json.loads(self.routes[path][1])
        payload["canonical_url"] = "/evidence/objects/stable-object?annotation=other"
        routes = {**self.routes, path: (200, json.dumps(payload).encode(), {})}
        with generated_http(routes) as (base, _), self.assertRaises(HTTPReadbackError):
            verify_http(self.manifest, self.inventory, base)

    def test_selected_public_attribution_is_verified_without_private_observations(self):
        payload = {"speaker_label": "Synthetic Speaker", "session_date": "2026-01-01",
                   "session_date_text": "January 1, 2026", "session_date_precision": "day",
                   "attribution_mode": "named"}
        self.manifest["videos"][0]["publication_attribution_json"] = [
            {"start_ms": 0, "end_ms": None, "payload": payload}]
        paths = ["/api/v1/public/evidence/objects/stable-object",
                 "/api/v1/public/evidence/objects/stable-object?annotation_id=stable-annotation"]
        for path in paths:
            evidence = json.loads(self.routes[path][1])
            evidence["citation_attribution"] = payload
            evidence["citation_attribution_timeline"] = [{"video_id": "generated-video", "start_ms": 0,
                "end_ms": None, **{key: payload[key] for key in ("speaker_label", "session_date", "session_date_text")}}]
            self.routes[path] = (200, json.dumps(evidence).encode(), {})
        with generated_http(self.routes) as (base, _): verify_http(self.manifest, self.inventory, base)
        # A stale label or extra private field on either public surface fails.
        for key, value in (("speaker_label", "Stale Speaker"), ("raw_text", "private generated observation")):
            routes = deepcopy(self.routes)
            evidence = json.loads(routes[paths[0]][1]); evidence["citation_attribution"][key] = value
            routes[paths[0]] = (200, json.dumps(evidence).encode(), {})
            with self.subTest(key=key), generated_http(routes) as (base, _), self.assertRaises(HTTPReadbackError):
                verify_http(self.manifest, self.inventory, base)

    def test_withdrawn_public_attribution_rejects_stale_target_timeline(self):
        path = "/api/v1/public/evidence/objects/stable-object"
        evidence = json.loads(self.routes[path][1])
        evidence["citation_attribution_timeline"] = [{"speaker_label": "Stale Speaker"}]
        routes = {**self.routes, path: (200, json.dumps(evidence).encode(), {})}
        with generated_http(routes) as (base, _), self.assertRaises(HTTPReadbackError):
            verify_http(self.manifest, self.inventory, base)

    def test_actual_evidence_wire_shape_omits_declared_nullable_fields(self):
        payload = {"speaker_label": None, "session_date": "2026-01-01", "session_date_text": None,
                   "session_date_precision": "day", "attribution_mode": "unavailable"}
        self.manifest["videos"][0]["publication_attribution_json"] = [
            {"start_ms": 0, "end_ms": None, "payload": payload}]
        for path in ("/api/v1/public/evidence/objects/stable-object",
                     "/api/v1/public/evidence/objects/stable-object?annotation_id=stable-annotation"):
            evidence = json.loads(self.routes[path][1])
            evidence["citation_attribution"] = {k: v for k, v in payload.items() if v is not None}
            evidence["citation_attribution_timeline"] = [{"video_id": "generated-video", "start_ms": 0,
                                                         "session_date": "2026-01-01"}]
            self.routes[path] = (200, json.dumps(evidence).encode(), {})
        with generated_http(self.routes) as (base, _): verify_http(self.manifest, self.inventory, base)

    def test_cross_object_video_annotation_requires_preview_but_not_unsupported_stable_focus(self):
        # Both objects belong to one selected project. A valid annotation on B
        # refers to A's video. Keep it in B's preview; stable evidence focuses
        # B's own video and same-object annotation only.
        self.manifest["projects"] = [{"id": "project"}]
        self.manifest["objects"][0]["project_id"] = "project"
        self.manifest["objects"].append({"id": "other-object", "project_id": "project", "is_embed_ready": False})
        self.manifest["videos"].append({"id": "other-video", "object_id": "other-object",
            "stable_video_id": "other-stable-video", "public_media_relpath": "other.mp4", "publication_attribution_json": []})
        self.inventory["other.mp4"] = self.inventory["video.mp4"].copy()
        cross = {**self.manifest["object_model_annotations"][0], "id": "cross", "video_id": "other-video",
                 "title": "Cross-object observation", "website_annotation_id": "cross-public"}
        self.manifest["object_model_annotations"].append(cross)
        preview_path = "/api/v1/public/objects/object/preview"
        preview = json.loads(self.routes[preview_path][1]); preview["annotations"].append(cross)
        self.routes[preview_path] = (200, json.dumps(preview).encode(), {})
        self.routes["/api/v1/public/objects"] = (200, b'[{"id":"object"},{"id":"other-object"}]', {})
        self.routes["/api/v1/public/objects/other-object/preview"] = (200,
            b'{"object":{"id":"other-object"},"annotations":[]}', {})
        self.routes["/api/v1/public/videos/other-video/stream"] = (200, self.video, {})
        with generated_http(self.routes) as (base, requests): verify_http(self.manifest, self.inventory, base)
        paths = [path for path, _ in requests]
        self.assertIn("/api/v1/public/evidence/objects/stable-object?annotation_id=stable-annotation", paths)
        self.assertNotIn("/api/v1/public/evidence/objects/stable-object?annotation_id=cross-public", paths)
        # Skipping an unsupported stable URL never permits dropping the valid
        # cross-object annotation from the verified preview.
        preview["annotations"].remove(cross)
        self.routes[preview_path] = (200, json.dumps(preview).encode(), {})
        with generated_http(self.routes) as (base, _), self.assertRaises(HTTPReadbackError):
            verify_http(self.manifest, self.inventory, base)

        # The preview-only form also remains supported when B has no own
        # eligible video at all. Its model and cross-object annotation still
        # require anonymous content checks; no stable B evidence is promised.
        self.manifest["videos"] = [video for video in self.manifest["videos"] if video["id"] != "video"]
        self.manifest["object_model_annotations"] = [cross]
        self.inventory.pop("video.mp4")
        preview["annotations"] = [cross]
        self.routes[preview_path] = (200, json.dumps(preview).encode(), {})
        with generated_http(self.routes) as (base, requests): verify_http(self.manifest, self.inventory, base)
        self.assertFalse(any(path.startswith("/api/v1/public/evidence/objects/stable-object") for path, _ in requests))

    def test_unselected_private_annotation_is_rejected_not_silently_ignored(self):
        path = "/api/v1/public/objects/object/preview"
        preview = json.loads(self.routes[path][1])
        preview["annotations"].append({**preview["annotations"][0], "id": "private", "is_published": False})
        self.routes[path] = (200, json.dumps(preview).encode(), {})
        with generated_http(self.routes) as (base, _), self.assertRaises(HTTPReadbackError):
            verify_http(self.manifest, self.inventory, base)

    def test_empty_generation_still_verifies_anonymous_withdrawal_over_http(self):
        routes = {"/api/v1/public/objects": (200, b"[]", {})}
        with generated_http(routes) as (base, _):
            result = verify_http({"objects": []}, {}, base)
        self.assertEqual(result["resource_count"], 1)
        routes["/api/v1/public/objects"] = (200, b'[{"id":"stale-object"}]', {})
        with generated_http(routes) as (base, _), self.assertRaises(HTTPReadbackError):
            verify_http({"objects": []}, {}, base)

    def test_changed_annotation_or_citation_refused(self):
        for path, payload in (
            ("/api/v1/public/objects/object/preview", {"object": {"id": "object"}, "annotations": []}),
            ("/api/v1/public/evidence/objects/stable-object", {"object": {"id": "wrong"}}),
            ("/api/v1/public/evidence/objects/stable-object?annotation_id=stable-annotation",
             {"selected_annotation": {"id": "different"}})):
            with self.subTest(path=path):
                routes = {**self.routes, path: (200, json.dumps(payload).encode(), {})}
                with generated_http(routes) as (base, _), self.assertRaises(HTTPReadbackError):
                    verify_http(self.manifest, self.inventory, base)

    def test_json_and_deadline_bounds(self):
        routes = {**self.routes, "/api/v1/public/objects/object/preview": (200, b"x" * (4 * 1024 * 1024 + 1), {})}
        with generated_http(routes) as (base, _), self.assertRaises(HTTPReadbackError):
            verify_http(self.manifest, self.inventory, base)
        with generated_http(self.routes) as (base, _), self.assertRaises(HTTPReadbackError):
            verify_http(self.manifest, self.inventory, base, deadline_seconds=0)

    def test_public_origin_configuration_refuses_credentials_paths_queries_and_other_schemes(self):
        for value in ("file:///private", "https://user:secret@example.invalid", "https://example.invalid/api",
                      "https://example.invalid?token=secret", "https://example.invalid#fragment",
                      "http://example.invalid:bad", "http://example.invalid:99999", "http://example.invalid:0",
                      "http://example.invalid:", "https://example.invalid\n"):
            with self.subTest(value=value), self.assertRaises(HTTPReadbackError):
                public_base(value)


if __name__ == "__main__":
    unittest.main()
