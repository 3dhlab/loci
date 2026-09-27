"""Fail-closed target checks for disposable CI databases and local Docker QA."""
from __future__ import annotations

import os
import sys
from urllib.parse import urlsplit


def _local_host_allowed(host: str | None, environ: dict, service: str) -> bool:
    if environ.get("SEMANTIC_ENV", "").lower() in {"production", "prod", "public"}:
        return False
    if host in {"127.0.0.1", "localhost", "::1"}:
        return True
    return environ.get("CI_DISPOSABLE_SERVICES") == "true" and host == service


def require_postgres_target(raw_url: str, *, migration: bool = False, environ=None) -> None:
    env = os.environ if environ is None else environ
    try:
        target = urlsplit(raw_url)
        databases = {"semantic_ci_migration"} if migration else {"semantic_ci", "loci_correction_qa"}
        valid = (
            target.scheme in {"postgresql", "postgresql+psycopg"}
            and _local_host_allowed(target.hostname, env, "postgres")
            and bool(target.username)
            and target.port in {None, 5432}
            and target.path.removeprefix("/") in databases
            and not target.query
            and not target.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Refusing PostgreSQL target: use the named disposable CI database on loopback, or explicitly opt into the local postgres test service; production mode is forbidden")


def require_redis_target(raw_url: str, *, environ=None) -> None:
    env = os.environ if environ is None else environ
    try:
        target = urlsplit(raw_url)
        valid = (
            target.scheme == "redis"
            and _local_host_allowed(target.hostname, env, "redis")
            and target.port in {None, 6379}
            and target.path == "/15"
            and not target.query
            and not target.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Refusing Redis target: use disposable Redis database 15 on loopback or the explicitly enabled local redis test service")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or sys.argv[1] != "migration":
            raise ValueError("Usage: disposable_targets.py migration")
        require_postgres_target(os.environ.get("CI_MIGRATION_DATABASE_URL", ""), migration=True)
    except ValueError as error:
        raise SystemExit(str(error)) from None
