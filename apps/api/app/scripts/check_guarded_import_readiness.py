"""Read-only pre-flight readiness checker for the guarded title-card import lane.

Operator pre-check for the FIRST guarded production import. It re-uses the SAME
helpers the server uses (`confine_import_path`, `compute_sha256`,
`production_imports_enabled`) so what it verifies is exactly what the import
route will re-verify server-side — no drift between the check and the gate.

Strictly local/staging-only and STRICTLY READ-ONLY:
- never opens a database connection, never writes a row, never runs an import;
- only stats files, hashes the staged fixture/media, and reports env posture;
- exits non-zero if any REQUIRED check fails, so it is machine-checkable in a
  shell gate (``&&`` chain) or CI.

It does NOT replace the server-side preflight (`POST .../preflight`); it is the
cheap filesystem/identity pre-check an operator runs before staging an import.

Examples
--------
Verify a staged fixture against its approved readiness-snapshot SHA::

    python -m app.scripts.check_guarded_import_readiness \
        --fixture demo-bowl-5s-speaker-approved-v1.json \
        --expected-fixture-sha <snapshot.fixture_sha256> \
        --import-root /var/lib/semantic/import \
        --backup-root /var/lib/semantic/import/backups

Also verify the source media hash and require the production flag to be OFF::

    python -m app.scripts.check_guarded_import_readiness \
        --fixture demo-bowl-5s-speaker-approved-v1.json \
        --expected-fixture-sha <sha> \
        --media /var/lib/semantic/media/demo-bowl.mp4 \
        --media-root /var/lib/semantic/media \
        --expected-media-sha <batch.source_media_sha256> \
        --require-production-off
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.api.v1.endpoints.authoring.imports import (
    PathConfinementError,
    compute_sha256,
    confine_import_path,
    production_imports_enabled,
    validate_source_version,
)


class _Checklist:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def add(self, name: str, ok: bool, detail: str, *, required: bool = True) -> None:
        self.rows.append({"name": name, "status": "pass" if ok else ("fail" if required else "warn"),
                          "detail": detail, "required": required})

    def info(self, name: str, detail: str) -> None:
        self.rows.append({"name": name, "status": "info", "detail": detail, "required": False})

    def passed(self) -> bool:
        return all(r["status"] != "fail" for r in self.rows)


def _check_dir(cl: _Checklist, name: str, path: str, *, must_write: bool) -> None:
    p = Path(path)
    if not p.exists():
        cl.add(name, False, f"{path} does not exist")
        return
    if not p.is_dir():
        cl.add(name, False, f"{path} is not a directory")
        return
    if must_write and not os.access(p, os.W_OK):
        cl.add(name, False, f"{path} is not writable")
        return
    cl.add(name, True, f"{path} present" + (" and writable" if must_write else ""))


def _check_confined_file(cl: _Checklist, name: str, candidate: str, root: str) -> Path | None:
    try:
        resolved = confine_import_path(candidate, root)
    except PathConfinementError as exc:
        cl.add(name + "_confined", False, str(exc))
        return None
    cl.add(name + "_confined", True, f"resolves under {root}: {resolved}")
    if not resolved.is_file():
        cl.add(name + "_present", False, f"{resolved} not found on disk")
        return None
    cl.add(name + "_present", True, f"{resolved} present")
    return resolved


def _check_sha(cl: _Checklist, name: str, path: Path, expected: str) -> None:
    actual = compute_sha256(path)
    ok = actual.lower() == expected.strip().lower()
    cl.add(name + "_sha_matches", ok,
           f"expected {expected.strip().lower()[:12]}… got {actual[:12]}…" if not ok
           else f"SHA-256 matches approved binding ({actual[:12]}…)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fixture", required=True, help="Staged fixture path (relative to --import-root).")
    parser.add_argument("--expected-fixture-sha", required=True,
                        help="The readiness snapshot's bound fixture_sha256 (64-hex).")
    parser.add_argument("--import-root", default=os.environ.get("AUTHORING_IMPORT_ROOT", "/var/lib/semantic/import"))
    parser.add_argument("--backup-root", default=os.environ.get("AUTHORING_IMPORT_BACKUP_ROOT",
                                                                "/var/lib/semantic/import/backups"))
    parser.add_argument("--source-version", default=None, help="Optional: validate the source_version shape.")
    parser.add_argument("--media", default=None, help="Optional source media path to hash.")
    parser.add_argument("--media-root", default=os.environ.get("AUTHORING_MEDIA_ROOT", "/var/lib/semantic/media"))
    parser.add_argument("--expected-media-sha", default=None, help="Optional: batch.source_media_sha256 (64-hex).")
    parser.add_argument("--require-production-off", action="store_true",
                        help="Fail if AUTHORING_IMPORT_PRODUCTION_ENABLED is currently on (default: report only).")
    parser.add_argument("--json", action="store_true", help="Emit the checklist as JSON.")
    args = parser.parse_args()

    cl = _Checklist()

    # 0. Production flag posture (informational unless --require-production-off).
    enabled = production_imports_enabled()
    if args.require_production_off:
        cl.add("production_flag_off", not enabled,
               "AUTHORING_IMPORT_PRODUCTION_ENABLED is OFF" if not enabled
               else "production flag is ON — refusing in a pre-stage check")
    else:
        cl.info("production_flag", f"AUTHORING_IMPORT_PRODUCTION_ENABLED={'on' if enabled else 'off'}")

    # 1. Roots.
    _check_dir(cl, "import_root", args.import_root, must_write=False)
    _check_dir(cl, "backup_root", args.backup_root, must_write=True)

    # 2. Optional source_version shape.
    if args.source_version is not None:
        cl.add("source_version_shape_valid", validate_source_version(args.source_version),
               f"source_version={args.source_version!r}")

    # 3. Fixture confinement + presence + SHA.
    fixture = _check_confined_file(cl, "fixture", args.fixture, args.import_root)
    if fixture is not None:
        _check_sha(cl, "fixture", fixture, args.expected_fixture_sha)

    # 4. Optional media confinement + SHA.
    if args.media is not None:
        if args.expected_media_sha is None:
            cl.add("media_expected_sha_provided", False,
                   "--media given without --expected-media-sha")
        else:
            media = _check_confined_file(cl, "media", args.media, args.media_root)
            if media is not None:
                _check_sha(cl, "media", media, args.expected_media_sha)

    ok = cl.passed()
    if args.json:
        print(json.dumps({"passed": ok, "checks": cl.rows}, indent=2))
    else:
        for r in cl.rows:
            mark = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "info": "INFO"}[r["status"]]
            print(f"[{mark}] {r['name']}: {r['detail']}")
        print(f"\n{'READY' if ok else 'NOT READY'} — guarded import pre-stage checklist")

    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
