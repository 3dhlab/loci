"""Small generated fixture for an explicitly disposable two-sided CI journey.

Importing this module is stdlib-only and does not connect, seed or encode. The
controller must migrate the two named disposable databases first. This script
inserts into an EMPTY source only; normal projection import creates the target's
inactive public owner. It never clears tables, seeds the target or starts a
service. Media is original generated geometry/colour, with no collection assets.

Run ``python scripts/ci/publication_acceptance_fixture.py seed`` only from the
approved CI controller. ``expectations`` prints public synthetic identifiers and
paths without requiring any service. Neither command prints credentials.

Seeding additionally requires Linux on an explicitly marked GitHub-hosted
Actions runner. Environment/URL names are eligibility checks, not runtime
ownership proof: the outer controller must first independently verify and retain
its owned Docker IDs, labels, networks and loopback endpoint receipts. This
standalone helper does not adopt or authenticate an ambient database/runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from urllib.parse import urlencode, urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5


ROOT = Path(__file__).resolve().parents[2]
NAMES = ("owner", "other", "project", "object", "private_object", "model",
         "private_model", "video", "private_video", "transcript", "segment1",
         "segment2", "segment3", "clip1", "clip2", "annotation",
         "private_annotation", "title_card")
IDS = {name: str(uuid5(NAMESPACE_URL, "https://example.invalid/loci/ci-publication/v1/" + name))
       for name in NAMES}
SLUG = "synthetic-publication-cube"
PRIVATE_SLUG = "synthetic-private-cube"
VIDEO_SLUG = "synthetic-publication-colours"
ANNOTATION_SLUG = "synthetic-two-view-annotation"
PRIVATE_ANNOTATION_SLUG = "synthetic-private-annotation"
CLIP_SLUGS = ("synthetic-first-view", "synthetic-last-view")
EMAILS = {"owner": "publication-owner@example.invalid", "other": "publication-other@example.invalid"}
PUBLIC_ATTRIBUTION = {"speaker_label": "Synthetic Public Speaker", "session_date": "2026-01-02",
                      "session_date_text": "January 2, 2026", "session_date_precision": "day",
                      "attribution_mode": "named"}
PRIVATE_MARKERS = ("CI_PRIVATE_OBJECT_CONTENT", "CI_PRIVATE_ANNOTATION_CONTENT",
                   "CI_PRIVATE_RAW_PRESENTER", "CI_PRIVATE_RAW_OCR", "CI_PRIVATE_RAW_PAYLOAD")
TEXTS = ("Synthetic front face of an original cube.", "Synthetic top edge of an original cube.",
         "Synthetic last view of an original cube.")


@dataclass(frozen=True)
class FixtureEnvironment:
    source_url: str = field(repr=False)
    target_url: str = field(repr=False)
    source_media: Path
    target_media: Path
    password: str = field(repr=False)


def _database(raw, database, env):
    try:
        parsed = urlsplit(raw)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        service = env.get("CI_DISPOSABLE_SERVICES") == "true" and parsed.hostname == "postgres"
        if (parsed.scheme not in {"postgresql", "postgresql+psycopg"}
                or not (local or service) or not parsed.username or parsed.path != "/" + database
                or parsed.query or parsed.fragment or any(ord(c) < 33 for c in raw)
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)
                or parsed.netloc.endswith(":")):
            raise ValueError()
        return raw
    except (TypeError, ValueError, AttributeError):
        raise ValueError("Use the explicitly named disposable publication database on loopback or the opted-in CI postgres service.") from None


def _private_directory(raw):
    try:
        path = Path(raw)
        if (not path.is_absolute() or ".." in path.parts or path.resolve(strict=True) != path
                or path == Path(path.anchor) or path == Path.home()
                or path.is_relative_to(ROOT)):
            raise ValueError()
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError()
        return path
    except (OSError, TypeError, ValueError):
        raise ValueError("Publication fixture media roots must be existing private, current-user-owned, canonical mode-0700 directories outside the checkout.") from None


def require_fixture_environment(environ=None):
    """Validate all targets before importing app code, SQL clients or encoders."""
    env = os.environ if environ is None else environ
    if (env.get("SEMANTIC_ENV") != "development"
            or env.get("LOCI_PUBLICATION_ACCEPTANCE_DISPOSABLE") != "true"):
        raise ValueError("Publication fixture requires explicit disposable opt-in and SEMANTIC_ENV=development.")
    source = _database(env.get("CI_PUBLICATION_SOURCE_DATABASE_URL", ""), "semantic_ci_publication_source", env)
    target = _database(env.get("CI_PUBLICATION_TARGET_DATABASE_URL", ""), "semantic_ci_publication_target", env)
    if env.get("DATABASE_URL") != source:
        raise ValueError("DATABASE_URL must exactly match the explicit disposable source target.")
    source_media = _private_directory(env.get("CI_PUBLICATION_SOURCE_MEDIA_ROOT", ""))
    target_media = _private_directory(env.get("CI_PUBLICATION_TARGET_MEDIA_ROOT", ""))
    if (source_media == target_media or source_media.parent != target_media.parent
            or env.get("MEDIA_ROOT") != str(source_media)):
        raise ValueError("Explicit source and target media roots must be distinct private siblings; MEDIA_ROOT must match source.")
    if (env.get("EMBEDDING_ENABLED") != "false" or env.get("EMBEDDING_WARMUP_ENABLED") != "false"
            or env.get("OPENAI_API_KEY", "") != ""):
        raise ValueError("Disable embedding and warmup and leave OPENAI_API_KEY empty for the generated fixture.")
    password = env.get("LOCI_PUBLICATION_TEST_PASSWORD", "")
    if (not isinstance(password, str) or not 32 <= len(password.encode("utf-8")) <= 72
            or any(ord(c) < 33 or ord(c) == 127 for c in password)):
        raise ValueError("Supply a generated synthetic fixture password of 32 to 72 non-control UTF-8 bytes.")
    return FixtureEnvironment(source, target, source_media, target_media, password)


def require_seed_eligibility(environ=None, *, platform=None):
    """CI-only seed gate; injectable inputs permit portable no-action tests."""
    env = os.environ if environ is None else environ
    host = sys.platform if platform is None else platform
    if (host != "linux" or env.get("GITHUB_ACTIONS") != "true"
            or env.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or env.get("CI_DISPOSABLE_SERVICES") != "true"
            or env.get("LOCI_PUBLICATION_ACCEPTANCE_DISPOSABLE") != "true"
            or env.get("SEMANTIC_ENV") != "development"):
        raise ValueError("Fixture seeding requires an explicitly opted-in Linux GitHub-hosted disposable Actions controller.")


def fixture_expectations():
    """Return only safe synthetic identities and approved public expectations."""
    base = "/api/v1/public/evidence/objects/" + SLUG
    return {"version": 1, "ids": {name: value for name, value in IDS.items() if name != "title_card"},
        "emails": dict(EMAILS), "public_attribution": dict(PUBLIC_ATTRIBUTION),
        "public_paths": {"objects": "/api/v1/public/objects", "evidence": base,
            "annotation": base + "?" + urlencode({"annotation_id": ANNOTATION_SLUG}),
            "clip": base + "?" + urlencode({"clip_id": CLIP_SLUGS[0]}),
            "preview": "/api/v1/public/objects/" + IDS["object"] + "/preview",
            "model": "/api/v1/public/objects/" + IDS["object"] + "/model/file",
            "video": "/api/v1/public/videos/" + IDS["video"] + "/stream"},
        "private_paths": ["/api/v1/public/objects/" + IDS["private_object"] + "/preview",
            "/api/v1/public/evidence/objects/" + PRIVATE_SLUG,
            "/api/v1/public/videos/" + IDS["private_video"] + "/stream",
            base + "?" + urlencode({"annotation_id": PRIVATE_ANNOTATION_SLUG})]}


def _generate_media(root):
    # These checked-in tiny assets were generated by generate_demo_assets.py.
    # The new source is a lavfi colour, not a re-tagged unknown research video.
    from app.scripts.generate_demo_assets import generate_clip
    from app.services.media_transcode import (probe_media, public_playback_transcode_command,
        require_public_playback_ffmpeg_runtime, run_transcode_process,
        transcode_output_dir, validate_public_playback_artifact, versioned_transcode_output_path)
    require_public_playback_ffmpeg_runtime()
    assets = root / "publication-acceptance"
    assets.mkdir(mode=0o700)
    fixtures = ROOT / "apps/web/tests/fixtures"
    for source, destination in (("synthetic-cube.glb", "cube.glb"), ("synthetic-poster.png", "poster.png")):
        shutil.copyfile(fixtures / source, assets / destination)
    source = assets / "original-blue.mp4"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
        "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=12:d=12", "-an",
        "-vf", "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "1",
        "-color_range", "tv", "-colorspace", "bt709", "-color_trc", "bt709",
        "-color_primaries", "bt709", "-movflags", "+faststart", str(source)]
    subprocess.run(command, check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    output = transcode_output_dir()
    if not output.resolve().is_relative_to(root):
        raise ValueError("Configured transcode directory must remain inside disposable source media.")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    temporary = output / "fixture-normalized.mp4"
    source_probe = probe_media(source)
    command = public_playback_transcode_command(source, temporary, probe=source_probe)
    command[-1:-1] = ["-threads", "1"]
    result = run_transcode_process(command, timeout_seconds=90)
    if result.return_code:
        raise RuntimeError("Generated CI playback normalization failed.")
    playback = validate_public_playback_artifact(temporary, source_probe=source_probe)
    final = versioned_transcode_output_path(IDS["video"], playback.sha256, "1" * 32)
    temporary.rename(final)
    private_final = versioned_transcode_output_path(IDS["private_video"], playback.sha256, "2" * 32)
    shutil.copyfile(final, private_final)
    private_model = assets / "private-cube.glb"
    shutil.copyfile(assets / "cube.glb", private_model)
    clips = []
    for index, (start, end) in enumerate(((0, 4000), (8000, 12000)), 1):
        clip = assets / (f"clip-{index}.mp4")
        generate_clip(final, clip, start, end)
        transcript = clip.with_suffix(".transcript.txt")
        transcript.write_text(TEXTS[0 if index == 1 else 2] + "\n", encoding="utf-8")
        clips.append((clip, transcript))
    return {"assets": assets, "source": source, "playback": playback, "final": final,
            "private_final": private_final, "private_model": private_model, "clips": clips}


def seed_fixture():
    """Seed once; any collision/nonempty target fails without deleting anything."""
    require_seed_eligibility()
    approved = require_fixture_environment()
    # No application import or connection is allowed above this boundary.
    sys.path.insert(0, str(ROOT / "apps/api"))
    from sqlalchemy import select
    from app.core.config import settings
    from app.core.security import get_password_hash
    from app.db import base  # Register every FK model before ORM use. noqa: F401
    from app.db.session import SessionLocal
    from app.models import entities as models
    from app.models.publication_job import PublicationDestinationHead, PublicationGeneration, PublicationJob
    if (settings.database_url != approved.source_url or settings.semantic_env != "development"
            or Path(settings.media_root) != approved.source_media):
        raise ValueError("Effective application settings differ from the guarded disposable source.")
    with SessionLocal() as db:
        has_history = any(db.execute(select(model).limit(1)).first() is not None
                          for model in (models.User, PublicationJob, PublicationGeneration, PublicationDestinationHead))
        if has_history or any(approved.source_media.iterdir()) or any(approved.target_media.iterdir()):
            raise ValueError("Publication acceptance requires empty source users and empty media roots; existing content is preserved.")
        media = _generate_media(approved.source_media)
        password_hash = get_password_hash(approved.password)
        for role in ("owner", "other"):
            db.add(models.User(id=UUID(IDS[role]), email=EMAILS[role], password_hash=password_hash,
                               is_active=True, is_platform_admin=False))
        db.flush()
        db.add(models.Project(id=UUID(IDS["project"]), owner_id=UUID(IDS["owner"]),
            name="Synthetic publication acceptance", description="Original generated geometry only.",
            website_slug="synthetic-publication"))
        db.flush()
        for name, published, slug, title in (("object", True, SLUG, "Synthetic publication cube"),
                ("private_object", False, PRIVATE_SLUG, PRIVATE_MARKERS[0])):
            db.add(models.Object(id=UUID(IDS[name]), project_id=UUID(IDS["project"]), name=title,
                description="Generated CI content.", website_object_id=slug, is_published=published,
                is_embed_ready=published, metadata_json={"public_standfirst": "Two views of generated geometry."}))
        db.flush()
        playback = media["playback"]
        source_sha = hashlib.sha256(media["source"].read_bytes()).hexdigest()
        for name, obj, stable, final in (("video", "object", VIDEO_SLUG, media["final"]),
                ("private_video", "private_object", "synthetic-private-colours", media["private_final"])):
            db.add(models.Video(id=UUID(IDS[name]), project_id=UUID(IDS["project"]), object_id=UUID(IDS[obj]),
                stable_video_id=stable, title="Synthetic colour demonstration", original_filename="original-blue.mp4",
                source_path=str(media["source"]), sha256_checksum=source_sha,
                playback_sha256_checksum=playback.sha256, playback_file_size_bytes=playback.size_bytes,
                playback_recipe_version=playback.recipe_revision, playback_color_policy=playback.color_policy,
                playback_storage_key=final.name, duration_ms=12000, status=models.VideoStatus.READY,
                is_published=True))
        for name, obj, path in (("model", "object", media["assets"] / "cube.glb"),
                ("private_model", "private_object", media["private_model"])):
            db.add(models.ObjectModel(id=UUID(IDS[name]), object_id=UUID(IDS[obj]), storage_path=str(path),
                original_filename="cube.glb", mime_type="model/gltf-binary",
                sha256_checksum=hashlib.sha256(path.read_bytes()).hexdigest(), file_size_bytes=path.stat().st_size,
                revision_number=1, default_camera_json={"position": [2, 1.5, 2], "target": [0, 0, 0]},
                public_poster_path=str(media["assets"] / "poster.png"), is_published=True,
                uploaded_by=UUID(IDS["owner"])))
        db.flush()
        vtt = "WEBVTT\n\n" + "\n\n".join(f"00:00:{i*4:02d}.000 --> 00:00:{(i+1)*4:02d}.000\n{text}"
                                                   for i, text in enumerate(TEXTS)) + "\n"
        db.add(models.Transcript(id=UUID(IDS["transcript"]), video_id=UUID(IDS["video"]),
            title="Synthetic cube transcript", source=models.TranscriptSource.MANUAL,
            format=models.TranscriptFormat.VTT, language="en", raw_text=vtt, is_published=True))
        db.flush()
        for index, text in enumerate(TEXTS, 1):
            db.add(models.Segment(id=UUID(IDS[f"segment{index}"]), transcript_id=UUID(IDS["transcript"]),
                position=index-1, start_ms=(index-1)*4000, end_ms=index*4000, text=text))
        for index, (path, transcript) in enumerate(media["clips"], 1):
            start, end = (0, 4000) if index == 1 else (8000, 12000)
            db.add(models.Clip(id=UUID(IDS[f"clip{index}"]), video_id=UUID(IDS["video"]),
                project_id=UUID(IDS["project"]), start_ms=start, end_ms=end,
                output_mp4_path=str(path), metadata_json_path=str(transcript), website_clip_id=CLIP_SLUGS[index-1],
                transcript_excerpt=TEXTS[0 if index == 1 else 2], citation_text=f"Synthetic cube section {index}.",
                status=models.ClipStatus.COMPLETE))
        db.flush()
        playlist = [{"video_id": IDS["video"], "clip_id": IDS[f"clip{i}"],
            "transcript_segment_id": IDS[f"segment{1 if i == 1 else 3}"],
            "start_ms": 0 if i == 1 else 8000, "end_ms": 4000 if i == 1 else 12000,
            "label": "Synthetic first view" if i == 1 else "Synthetic last view"} for i in (1, 2)]
        for name, published, slug, title in (("annotation", True, ANNOTATION_SLUG, "Synthetic two-view annotation"),
                ("private_annotation", False, PRIVATE_ANNOTATION_SLUG, PRIVATE_MARKERS[1])):
            db.add(models.ObjectModelAnnotation(id=UUID(IDS[name]), object_model_id=UUID(IDS["model"]),
                object_id=UUID(IDS["object"]), video_id=UUID(IDS["video"]), clip_id=UUID(IDS["clip1"]),
                transcript_segment_id=UUID(IDS["segment1"]), website_annotation_id=slug, title=title,
                description="Two non-contiguous original synthetic views.", point_x=0, point_y=0, point_z=.5,
                playlist_json=playlist, start_ms=0, end_ms=4000, is_published=published,
                model_revision_created_against=1, created_by=UUID(IDS["owner"])))
        db.add(models.TitleCardObservation(id=UUID(IDS["title_card"]), video_id=UUID(IDS["video"]),
            timestamp_ms=0, title_card_visible=True, presenter_name=PRIVATE_MARKERS[2],
            public_speaker_label=PUBLIC_ATTRIBUTION["speaker_label"], session_date=date(2026, 1, 2),
            session_date_text=PUBLIC_ATTRIBUTION["session_date_text"], raw_text_observed=PRIVATE_MARKERS[3],
            raw_payload_json={"synthetic_private": PRIVATE_MARKERS[4]}, confidence_presenter="high",
            confidence_session_date="high", prompt_version="synthetic-ci-v1", source_kind="vision_ocr",
            source_version="synthetic-ci-v1", status="ready"))
        db.commit()
    return fixture_expectations()


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def _assert_no_private(value):
    encoded = json.dumps(value, sort_keys=True)
    _require(not any(marker in encoded for marker in PRIVATE_MARKERS), "Synthetic private fixture content leaked.")
    _require(not any(key in encoded for key in ("raw_text_observed", "raw_payload_json", "presenter_name")),
             "Raw title-card fields leaked.")


def assert_projection(manifest, media_paths):
    """Assert fixture coverage/privacy before admitting its real publication."""
    expected = {"projects": ["project"], "objects": ["object"], "videos": ["video"],
        "transcripts": ["transcript"], "segments": ["segment1", "segment2", "segment3"],
        "object_models": ["model"], "clips": ["clip1", "clip2"], "object_model_annotations": ["annotation"]}
    for collection, names in expected.items():
        rows = manifest.get(collection, [])
        _require(isinstance(rows, list) and len(rows) == len(names)
                 and {row.get("id") for row in rows} == {IDS[name] for name in names},
                 "Unexpected synthetic projection selection: " + collection)
    _assert_no_private(manifest)
    _require("title_card_observations" not in manifest, "Raw title-card collection entered projection.")
    video = manifest["videos"][0]
    _require(video.get("publication_attribution_json") == [{"start_ms": 0, "end_ms": None,
        "payload": PUBLIC_ATTRIBUTION}], "Approved public attribution was not frozen exactly.")
    _require(len(manifest["object_model_annotations"][0].get("playlist_json", [])) == 2,
             "Synthetic publication must exercise the two-clip playlist.")
    paths = set(media_paths)
    required = {video["public_media_relpath"], manifest["object_models"][0]["public_media_relpath"],
                manifest["object_models"][0]["public_poster_relpath"]}
    for clip in manifest["clips"]:
        required.update(clip[name] for name in ("public_media_relpath", "public_poster_relpath", "public_transcript_relpath"))
    _require(paths == required and set(manifest.get("media_paths", [])) == paths and len(paths) == 9,
             "Synthetic media selection must include only the nine public model/poster/playback/clip/transcript files.")
    _require(not any("private" in path or IDS["private_video"] in path for path in paths), "Private media entered selection.")
    _require(IDS["video"] in video["public_media_relpath"], "Versioned playback identity was not selected.")


def assert_public_snapshot(objects, evidence, private_statuses):
    """Check already-fetched real HTTP JSON/statuses, without performing I/O."""
    _require(isinstance(objects, list) and len(objects) == 1 and objects[0].get("id") == IDS["object"],
             "Anonymous listing must contain only the synthetic published object.")
    _assert_no_private([objects, evidence])
    _require(evidence.get("object", {}).get("id") == SLUG, "Stable public object identity differs.")
    _require(evidence.get("playback", {}).get("video_id") == VIDEO_SLUG, "Stable public video identity differs.")
    _require({row.get("id") for row in evidence.get("annotations", [])} == {ANNOTATION_SLUG},
             "Anonymous annotations must exclude the synthetic private annotation.")
    _require({row.get("id") for row in evidence.get("clips", [])} == set(CLIP_SLUGS), "Public clips differ.")
    _require([row.get("text") for row in (evidence.get("transcript") or {}).get("segments", [])] == list(TEXTS),
             "Public transcript differs.")
    attribution = evidence.get("citation_attribution") or {}
    _require({key: attribution.get(key) for key in PUBLIC_ATTRIBUTION} == PUBLIC_ATTRIBUTION,
             "Public attribution differs from the approved synthetic fields.")
    expected_paths = set(fixture_expectations()["private_paths"])
    _require(set(private_statuses) == expected_paths and all(type(value) is int and value == 404
             for value in private_statuses.values()), "Every private object/video/annotation route must return 404.")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"seed", "expectations"}:
        raise SystemExit("Usage: publication_acceptance_fixture.py seed|expectations")
    try:
        result = seed_fixture() if sys.argv[1] == "seed" else fixture_expectations()
    except (ValueError, AssertionError) as error:
        raise SystemExit(str(error)) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
