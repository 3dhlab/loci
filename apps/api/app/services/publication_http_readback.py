"""Anonymous HTTP readback of selected public bytes and stable evidence routes."""
from datetime import datetime, timezone
import hashlib
import json
import time
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from app.services.publication_readback import digest


class HTTPReadbackError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def public_base(value):
    try:
        if not isinstance(value, str) or any(ord(c) < 33 or ord(c) == 127 for c in value):
            raise ValueError()
        parsed = urlsplit(value)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)
                or parsed.netloc.endswith(":")):
            raise ValueError()
        return value.rstrip("/")
    except (TypeError, ValueError):
        raise HTTPReadbackError("A trusted public HTTP origin must be configured.") from None


def verify_http(manifest, inventory, base_url, *, opener=None, deadline_seconds=1200):
    """Read without credentials, redirects or environment proxy inheritance.

    Stream media with exact selected sizes/hashes. JSON bodies are bounded and
    checked for annotation identity/content and stable citation focus. This is
    HTTP acceptance; real browser rendering remains a separate release check.
    """
    base = public_base(base_url)
    opener = opener or build_opener(ProxyHandler({}), _NoRedirect())
    deadline = time.monotonic() + deadline_seconds
    observations = []

    def fetch(path, expected=None):
        if not path.startswith("/") or path.startswith("//"):
            raise HTTPReadbackError("Invalid public readback route.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HTTPReadbackError("Public readback deadline reached.")
        request = Request(base + path, headers={"Cache-Control": "no-cache", "Accept-Encoding": "identity"})
        try:
            with opener.open(request, timeout=min(30, remaining)) as response:
                if response.status != 200 or response.geturl() != base + path:
                    raise HTTPReadbackError("Public readback changed route or status.")
                limit = expected["size_bytes"] if expected is not None else 4 * 1024 * 1024
                total = 0
                checksum = hashlib.sha256()
                body = bytearray()
                while True:
                    if time.monotonic() >= deadline:
                        raise HTTPReadbackError("Public readback deadline reached.")
                    chunk = response.read(min(1024 * 1024, max(1, limit - total + 1)))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise HTTPReadbackError("Public readback exceeds the selected size.")
                    checksum.update(chunk)
                    if expected is None:
                        body.extend(chunk)
                actual = checksum.hexdigest()
                if expected is not None and (total != expected["size_bytes"] or actual != expected["sha256"]):
                    raise HTTPReadbackError("Public media differs from its selected bytes.")
                observations.append({"path": path, "bytes": total, "sha256": actual})
                return json.loads(body) if expected is None else None
        except HTTPReadbackError:
            raise
        except Exception:
            raise HTTPReadbackError("Public HTTP readback could not be verified.") from None

    models = {r["object_id"]: r for r in manifest.get("object_models", [])}
    annotations = manifest.get("object_model_annotations", [])
    videos = manifest.get("videos", [])
    listing = fetch("/api/v1/public/objects")
    expected_ids = {r["id"] for r in manifest.get("objects", [])}
    if (not isinstance(listing, list) or len(listing) != len(expected_ids)
            or any(not isinstance(row, dict) for row in listing)
            or {row.get("id") for row in listing} != expected_ids):
        raise HTTPReadbackError("Public object selection differs from the committed generation.")
    for obj in manifest.get("objects", []):
        ident = quote(obj["id"], safe="")
        preview = fetch(f"/api/v1/public/objects/{ident}/preview")
        if preview.get("object", {}).get("id") != obj["id"]:
            raise HTTPReadbackError("Public object identity does not match.")
        for key in ("name", "description", "external_url", "is_published"):
            if key in obj and preview["object"].get(key) != obj[key]:
                raise HTTPReadbackError("Public object content differs.")
        selected = {r["id"]: r for r in annotations if r["object_id"] == obj["id"]}
        actual = preview.get("annotations")
        if (not isinstance(actual, list) or len(actual) != len(selected)
                or any(not isinstance(r, dict) for r in actual)
                or {r.get("id") for r in actual} != set(selected)):
            raise HTTPReadbackError("Public annotation selection differs.")
        for row in actual:
            expected = selected[row["id"]]
            for key in ("title", "description", "video_id", "start_ms", "end_ms", "review_status", "is_published",
                        "point_x", "point_y", "point_z", "normal_x", "normal_y", "normal_z", "camera_json",
                        "clip_id", "transcript_segment_id", "model_revision_created_against"):
                if key in expected and row.get(key) != expected[key]:
                    raise HTTPReadbackError("Public annotation content differs.")
            if "playlist_json" in expected and canonical_playlist(row.get("playlist")) != canonical_playlist(expected["playlist_json"]):
                raise HTTPReadbackError("Public annotation playlist differs.")
        model = models.get(obj["id"])
        if model:
            fetch(f"/api/v1/public/objects/{ident}/model/file", inventory[model["public_media_relpath"]])
        # Incomplete preview objects are intentionally not promised an evidence
        # page. Eligible stable evidence routes must resolve the selected focus.
        slug = obj.get("website_object_id")
        if (slug and obj.get("is_embed_ready") and model
                and any(r.get("object_id") == obj["id"] for r in videos)):
            path = f"/api/v1/public/evidence/objects/{quote(slug, safe='')}"
            evidence = fetch(path)
            if evidence.get("object", {}).get("id") != slug:
                raise HTTPReadbackError("Stable citation object identity differs.")
            verify_attribution(evidence, [v for v in videos if v.get("object_id") == obj["id"]])
            canonical_path = urlsplit(evidence.get("canonical_url", "")).path
            if canonical_path != f"/evidence/objects/{quote(slug, safe='')}":
                raise HTTPReadbackError("Stable citation route differs.")
            for row in selected.values():
                public_id = row.get("website_annotation_id")
                if not public_id:
                    continue
                focused = fetch(path + "?" + urlencode({"annotation_id": public_id}))
                verify_attribution(focused, [v for v in videos if v.get("object_id") == obj["id"]])
                picked = focused.get("selected_annotation") or {}
                if (picked.get("id") != public_id or picked.get("title") != row.get("title")
                        or picked.get("start_ms") != row.get("start_ms")
                        or picked.get("end_ms") != row.get("end_ms")
                        or not canonical_focus(focused, slug, "annotation", public_id)):
                    raise HTTPReadbackError("Stable annotation citation differs.")
            video_ids = {r["id"] for r in videos if r.get("object_id") == obj["id"]}
            selected_clips = [r for r in manifest.get("clips", [])
                              if r.get("website_clip_id") and r.get("video_id") in video_ids]
            if selected_clips:
                embed = fetch(f"/api/public/objects/{quote(slug, safe='')}")
                actual_clips = embed.get("clips") if isinstance(embed, dict) else None
                if (not isinstance(actual_clips, list) or len(actual_clips) != len(selected_clips)
                        or any(not isinstance(r, dict) for r in actual_clips)
                        or {r.get("id") for r in actual_clips} != {r["website_clip_id"] for r in selected_clips}):
                    raise HTTPReadbackError("Public citation clip selection differs.")
                by_id = {r["id"]: r for r in actual_clips}
                for clip in selected_clips:
                    citation = clip.get("citation_text")
                    expected_citation = (citation.strip() or None) if isinstance(citation, str) else None
                    if by_id[clip["website_clip_id"]].get("description") != expected_citation:
                        raise HTTPReadbackError("Public clip citation text differs.")
            for clip in selected_clips:
                clip_id = clip.get("website_clip_id")
                if not clip_id or clip.get("video_id") not in video_ids:
                    continue
                public_clip = quote(clip_id, safe="")
                fetch(f"/api/public/clips/{public_clip}/stream", inventory[clip["public_media_relpath"]])
                if clip.get("public_poster_relpath"):
                    fetch(f"/api/public/clips/{public_clip}/poster", inventory[clip["public_poster_relpath"]])
                focused = fetch(path + "?" + urlencode({"clip_id": clip_id}))
                verify_attribution(focused, [v for v in videos if v.get("object_id") == obj["id"]])
                picked = focused.get("selected_clip") or {}
                excerpts = manifest.get("public_clip_excerpts")
                if not isinstance(excerpts, dict) or clip["id"] not in excerpts:
                    raise HTTPReadbackError("Selected public clip excerpt is missing.")
                if (picked.get("id") != clip_id or picked.get("start_ms") != clip.get("start_ms")
                        or picked.get("end_ms") != clip.get("end_ms")
                        or picked.get("transcript_excerpt") != excerpts[clip["id"]]
                        or not canonical_focus(focused, slug, "clip", clip_id)):
                    raise HTTPReadbackError("Stable clip citation differs.")
    for video in videos:
        fetch(f"/api/v1/public/videos/{quote(video['id'], safe='')}/stream",
              inventory[video["public_media_relpath"]])
    return {"status": "verified", "resource_count": len(observations),
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "observations_sha256": digest(observations)}


def canonical_playlist(value):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise HTTPReadbackError("Public annotation playlist is invalid.")
    fields = ("video_id", "clip_id", "transcript_segment_id", "label", "start_ms", "end_ms")
    return [{field: row.get(field) for field in fields} for row in value]


def canonical_focus(evidence, slug, kind, ident):
    try:
        value = urlsplit(evidence.get("canonical_url", ""))
        return (not value.scheme and not value.netloc
                and value.path == f"/evidence/objects/{quote(slug, safe='')}"
                and not value.fragment and parse_qs(value.query) == {kind: [ident]})
    except (ValueError, TypeError):
        return False


def verify_attribution(evidence, videos):
    """Match the selected public-only timeline and focused attribution over HTTP."""
    from app.services.publication_attribution import validate_public_attribution_bands
    focus = evidence.get("focus") or {}
    stable_id = focus.get("video_id")
    matching = [video for video in videos if video.get("stable_video_id") == stable_id]
    if (len(matching) != 1 or not isinstance(stable_id, str)
            or (evidence.get("playback") or {}).get("video_id") != stable_id
            or type(focus.get("start_ms")) is not int or focus["start_ms"] < 0):
        raise HTTPReadbackError("Public citation playback focus differs.")
    try:
        bands = validate_public_attribution_bands(matching[0].get("publication_attribution_json"))
    except (ValueError, TypeError):
        raise HTTPReadbackError("Selected public attribution is invalid.") from None
    timeline = [{"video_id": stable_id, "start_ms": band["start_ms"], "end_ms": band["end_ms"],
                 **{key: band["payload"][key] for key in ("speaker_label", "session_date", "session_date_text")}}
                for band in bands]
    actual_timeline = evidence.get("citation_attribution_timeline")
    if not isinstance(actual_timeline, list):
        raise HTTPReadbackError("Public citation attribution timeline is invalid.")
    actual_timeline = [public_fields(row,
        {"video_id", "start_ms", "end_ms", "speaker_label", "session_date", "session_date_text"},
        {"video_id", "start_ms"}) for row in actual_timeline]
    if digest(actual_timeline) != digest(timeline):
        raise HTTPReadbackError("Public citation attribution timeline differs.")
    expected = None
    if bands:
        selected = next((band for band in bands if band["start_ms"] <= focus["start_ms"]
                        and (band["end_ms"] is None or focus["start_ms"] < band["end_ms"])),
                        min(bands, key=lambda band: abs(band["start_ms"] - focus["start_ms"])))
        expected = selected["payload"]
    actual = evidence.get("citation_attribution")
    if actual is not None:
        actual = public_fields(actual, {"speaker_label", "session_date", "session_date_text",
                               "session_date_precision", "attribution_mode"},
                               {"session_date_precision", "attribution_mode"})
    if digest(actual) != digest(expected):
        raise HTTPReadbackError("Public focused citation attribution differs.")


def public_fields(value, allowed, required):
    """Restore declared nullable fields omitted by exclude_none, reject extras."""
    if not isinstance(value, dict) or not set(value) <= allowed or not required <= set(value):
        raise HTTPReadbackError("Public attribution contains invalid fields.")
    return {field: value.get(field) for field in allowed}
