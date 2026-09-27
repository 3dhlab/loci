#!/usr/bin/env python3
"""Run every API test module in its own disposable, synthetic-only process."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from disposable_targets import require_postgres_target, require_redis_target

ROOT = Path(__file__).resolve().parents[2]
API = ROOT / "apps/api"
ARTIFACTS = Path(os.environ.get("CI_ARTIFACT_DIR", "/tmp/loci-api-ci"))
BASE_URL = os.environ.get("CI_DATABASE_URL", "")
REDIS_URL = os.environ.get("CI_REDIS_URL", "")

if not BASE_URL or not REDIS_URL:
    raise SystemExit("CI_DATABASE_URL and CI_REDIS_URL must name explicit disposable services")
try:
    require_postgres_target(BASE_URL)
    require_redis_target(REDIS_URL)
except ValueError as error:
    raise SystemExit(str(error)) from None
ARTIFACTS.mkdir(parents=True, exist_ok=True)

# Execute from an API-only copy with source and migrations preserved verbatim.
with tempfile.TemporaryDirectory(prefix="loci-api-ci-") as temp_name:
    sanitized = Path(temp_name) / "api"
    shutil.copytree(API, sanitized, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": BASE_URL,
        "SEMANTIC_TEST_BASE_DATABASE_URL": BASE_URL,
        "AUTHORING_LANE_TEST_BASE_DATABASE_URL": BASE_URL,
        "AUTHORING_API_TEST_BASE_DATABASE_URL": BASE_URL,
        "AUTHORING_APPROVAL_TEST_BASE_DATABASE_URL": BASE_URL,
        "AUTHORING_WRITE_TEST_BASE_DATABASE_URL": BASE_URL,
        "AUTHORING_IMPORT_TEST_BASE_DATABASE_URL": BASE_URL,
        "JWT_SECRET_KEY": "ci-synthetic-jwt-secret-at-least-32-bytes",
        "ENCRYPTION_MASTER_KEY": "ci-synthetic-encryption-key-only",
        "OPENAI_API_KEY": "",
        "EMBEDDING_WARMUP_ENABLED": "false",
        "REDIS_URL": os.environ.get("CI_REDIS_URL", "redis://127.0.0.1:6379/15"),
    })
    # Prove the disposable Redis service is reachable and usable, then leave no key.
    redis_check = subprocess.run([sys.executable, "-c", "import os,redis; r=redis.Redis.from_url(os.environ['REDIS_URL']); r.ping(); k='loci-ci-probe:'+str(os.getpid()); r.set(k,b'ok',ex=30); assert r.get(k)==b'ok'; r.delete(k)"], env=env, text=True, capture_output=True)
    if redis_check.returncode:
        print(redis_check.stdout, end="")
        print(redis_check.stderr, end="", file=sys.stderr)
        raise SystemExit("Disposable Redis preflight failed")

    test_files = sorted((sanitized / "tests").glob("test_*.py"))
    if not test_files:
        raise SystemExit("API test suite is empty")
    env["PYTHONPATH"] = str(sanitized) + os.pathsep + env.get("PYTHONPATH", "")
    skip_records: list[tuple[str, str]] = []
    failures: list[str] = []
    required_privacy_test_seen = False
    for test_file in test_files:
        report = ARTIFACTS / f"{test_file.stem}.xml"
        command = [sys.executable, "-m", "pytest", str(test_file), "-q", "--strict-markers", "--junitxml", str(report)]
        print(f"\n=== API module: {test_file.name} ===", flush=True)
        result = subprocess.run(command, cwd=sanitized, env=env)
        if result.returncode:
            failures.append(f"{test_file.name}: exit {result.returncode}")
            if not report.exists():
                continue
        root = ET.parse(report).getroot()
        for case in root.findall(".//testcase"):
            if case.get("name") == "test_public_evidence_page_exposes_public_safe_citation_attribution_timeline":
                required_privacy_test_seen = case.find("skipped") is None and case.find("failure") is None and case.find("error") is None
            skipped = case.find("skipped")
            if skipped is not None:
                skip_records.append((case.get("name", "unknown"), skipped.get("message", "")))

    unexpected = skip_records
    allowed_skip_names = set()
    expected_skips_missing = []
    summary = {"candidate": os.environ.get("GITHUB_SHA", "local-working-tree"), "apiModules": len(test_files), "allowedSkipped": [{"test": name, "reason": reason} for name, reason in skip_records], "unexpectedSkipped": unexpected, "expectedPrivateFixtureSkips": 0, "publicProjectionPrivacyTestPassed": required_privacy_test_seen, "fixturePolicy": "The public suite uses synthetic fixtures and permits no skipped cases. Projection privacy executes against PostgreSQL."}
    (ARTIFACTS / "api-suite-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    if unexpected:
        failures.append("unexpected API skip(s)")
    if not required_privacy_test_seen:
        failures.append("required real API public projection privacy test did not pass")
    if failures:
        raise SystemExit("API suite failed: " + "; ".join(failures) + "; see api-suite-summary.json")
