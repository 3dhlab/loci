from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.public_sitemap import build_public_sitemap_entries, render_public_sitemap_xml


def _default_site_base_url() -> str:
    candidate = (settings.semantic_embed_public_base_url or settings.api_base_url or "").strip()
    if not candidate:
        raise RuntimeError("A site base URL is required to export the public sitemap")
    return candidate


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the published public sitemap as XML.")
    parser.add_argument(
        "--site-base-url",
        default=_default_site_base_url(),
        help="Absolute site base URL used to build sitemap <loc> entries.",
    )
    parser.add_argument("--output", help="Optional output path. Defaults to stdout when omitted.")
    args = parser.parse_args()

    with SessionLocal() as db:
        xml_text = render_public_sitemap_xml(
            build_public_sitemap_entries(db),
            site_base_url=args.site_base_url,
        )

    if args.output:
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(xml_text, encoding="utf-8")
        return

    sys.stdout.write(xml_text)


if __name__ == "__main__":
    main()