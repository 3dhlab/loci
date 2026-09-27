from __future__ import annotations

import argparse
import json
import sys

from app.worker.jobs import ingest_title_card_ocr_job


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import or run title-card OCR observations for a video.",
        epilog=(
            "--replace-matching-source only prunes stale rows whose prompt version, "
            "source kind, and source version match the imported batch. It does not "
            "clear all title-card observations for the video."
        ),
    )
    parser.add_argument("--video-id", required=True, help="Video UUID to attach observations to.")
    parser.add_argument("--fixture", help="Path to structured OCR JSON to import deterministically.")
    parser.add_argument("--live", action="store_true", help="Run live OCR against sampled frames. Requires configured OpenAI credentials.")
    parser.set_defaults(replace_existing=False)
    parser.add_argument(
        "--replace-matching-source",
        dest="replace_existing",
        action="store_true",
        help=(
            "Replace stale observations only when prompt version, source kind, and "
            "source version match the imported batch."
        ),
    )
    parser.add_argument(
        "--replace-existing",
        dest="replace_existing",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    if args.live and args.fixture:
        parser.error("Use either --live or --fixture, not both.")
    if not args.live and not args.fixture:
        parser.error("Provide --fixture for deterministic import or pass --live for a real OCR run.")

    result = ingest_title_card_ocr_job(
        args.video_id,
        fixture_path=args.fixture,
        replace_existing=bool(args.replace_existing),
        run_live=bool(args.live),
    )
    print(json.dumps(result, indent=2, sort_keys=True))

    if result.get("status") != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()