"""Check the repository's own synthetic fixtures before public CI runs.

The checks are scoped to Loci's test/demo assets. Adopters' runtime media and
collection records are not inputs to this guard.
"""

from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = "apps/web/tests/fixtures/"
ALLOWED_FIXTURES = {
    f"{FIXTURE_DIR}synthetic-12s.mp4",
    f"{FIXTURE_DIR}synthetic-cube.glb",
    f"{FIXTURE_DIR}synthetic-poster.png",
}
MEDIA_SUFFIXES = {".mp4", ".mov", ".m4v", ".glb", ".gltf", ".png", ".jpg", ".jpeg", ".webp"}
PRIVATE_SUFFIXES = {".pem", ".key", ".p12", ".dump", ".sqlite", ".sqlite3"}
PUBLIC_DEMO_URL = "https://loci.threedeezy.com/public"
PUBLIC_DEMO_HOST = "loci.threedeezy.com"
SELF = "scripts/ci/check-public-fixtures.py"


def tracked_paths() -> list[str]:
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [path.decode() for path in output.split(b"\0") if path]


def check() -> list[str]:
    errors = []
    for relative in tracked_paths():
        path = Path(relative)
        lower = relative.lower()
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            errors.append(f"tracked private environment file: {relative}")
        if path.suffix.lower() in PRIVATE_SUFFIXES or any(part in {".media", ".backups"} for part in path.parts):
            errors.append(f"tracked runtime or private file: {relative}")
        if lower.startswith(FIXTURE_DIR) and path.suffix.lower() in MEDIA_SUFFIXES:
            if relative not in ALLOWED_FIXTURES:
                errors.append(f"new browser media fixture needs a generation recipe and review: {relative}")
            elif (ROOT / relative).stat().st_size > 1_000_000:
                errors.append(f"browser media fixture exceeds 1 MB: {relative}")

        if relative == SELF or path.suffix.lower() in MEDIA_SUFFIXES:
            continue
        content = (ROOT / relative).read_bytes()
        if b"\0" in content:
            continue
        if PUBLIC_DEMO_HOST.encode() in content:
            if relative != "README.md":
                errors.append(f"hosted research URL appears outside the documented README link: {relative}")
            else:
                urls = re.findall(rb"https://loci\.threedeezy\.com[^\s)<]*", content)
                if not urls or any(url.decode() != PUBLIC_DEMO_URL for url in urls):
                    errors.append("README must contain only the documented public hosted-demo URL")

    citation_test = (ROOT / "apps/web/src/lib/citationAttribution.test.js").read_text()
    for field, pattern in (
        ("collectionName", r"(?:Synthetic|Demo)"),
        ("speakerLabel", r"(?:|.* Example|Synthetic .*)"),
        ("speakerAuthorName", r"(?:|Example, .*|Synthetic .*)"),
    ):
        for value in re.findall(rf"\b{field}: '([^']*)'", citation_test):
            if not re.fullmatch(pattern, value) and not (field == "collectionName" and value.startswith("Synthetic ")):
                errors.append(f"{field} in citation test is not plainly synthetic: {value}")

    authoring_test = (ROOT / "apps/api/tests/test_authoring_api.py").read_text()
    for value in re.findall(r'stable_video_id="([^"]+)"', authoring_test):
        if not value.startswith("video-test-") or value != "video-test-sample":
            errors.append(f"authoring test video ID needs a synthetic value: {value}")
    return errors


if __name__ == "__main__":
    problems = check()
    if problems:
        print("\n".join(problems), file=sys.stderr)
        raise SystemExit(1)
    print("Public fixture boundary passed")
