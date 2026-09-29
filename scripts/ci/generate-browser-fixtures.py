"""Regenerate the browser regression's original geometric media fixtures.

Run from any directory with Python 3 and FFmpeg (including libx264) on PATH:
    python3 scripts/ci/generate-browser-fixtures.py

The same source generator creates the local demo's cube, three-color video,
and poster. These assets contain no research collection or participant media.
"""

from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
from app.scripts.generate_demo_assets import generate_assets  # noqa: E402


def main() -> None:
    destination = ROOT / "apps" / "web" / "tests" / "fixtures"
    destination.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="loci-browser-fixtures-") as temporary:
        generated = Path(temporary)
        generate_assets(generated)
        for source, target in (
            ("cube.glb", "synthetic-cube.glb"),
            ("colors.mp4", "synthetic-12s.mp4"),
            ("poster.png", "synthetic-poster.png"),
        ):
            shutil.copyfile(generated / source, destination / target)


if __name__ == "__main__":
    main()
