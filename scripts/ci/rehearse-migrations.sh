#!/usr/bin/env bash
set -euo pipefail
: "${CI_MIGRATION_DATABASE_URL:?CI_MIGRATION_DATABASE_URL is required}"
mkdir -p "${CI_ARTIFACT_DIR:-/tmp/loci-api-ci}"
python "$(dirname "$0")/disposable_targets.py" migration
cd "$(dirname "$0")/../../apps/api"
export DATABASE_URL="$CI_MIGRATION_DATABASE_URL"
python -m alembic upgrade head
python -m alembic current | tee "${CI_ARTIFACT_DIR:-/tmp/loci-api-ci}/migration-upgrade.txt"
python -m alembic downgrade base
python -m alembic current | tee "${CI_ARTIFACT_DIR:-/tmp/loci-api-ci}/migration-downgrade.txt"
python -m alembic upgrade head
python -m alembic current | tee "${CI_ARTIFACT_DIR:-/tmp/loci-api-ci}/migration-reupgrade.txt"
python -m alembic current | python -c "import sys; output=sys.stdin.read(); assert '20260813_0036' in output, output"
printf 'migration cycles passed through revision 20260813_0036\n'
