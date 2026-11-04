# Validation

Run checks against the exact checkout you intend to review or release. Record `git rev-parse HEAD`, the commands, their results and any blocked checks. [GitHub Actions](../.github/workflows/ci.yml) defines the four required jobs; [contribution guidance](../.github/CONTRIBUTING.md) explains the review and release process. Historical release results do not validate a newer checkout.

## Prerequisites

Run commands from the repository root unless shown otherwise.

- Web: Node 22, npm and the committed npm lockfile. Browser checks require Chromium through Playwright; Linux runners also need its system libraries.
- API: Python 3.12 in an isolated environment, FFmpeg, PostgreSQL 16 with pgvector and Redis 7.4. Use fresh disposable databases named `semantic_ci` and `semantic_ci_migration`, and a dedicated disposable Redis service using database 15.
- Running demo: Docker Engine with Compose v2 or Docker Desktop, Python 3 and the generated sample setup in the [quickstart](../README.md#quickstart).

Use generated content and test-only credentials. Keep private `.env` files, participant records, research media and operational captures out of source and test artifacts. The API and migration commands below modify their disposable targets; never point them at a research or hosted database.

## Web, browser and dependency checks

```sh
cd apps/web
npm ci --ignore-scripts --no-audit
npm test
npm run build -- --outDir dist-ci
npx playwright install chromium
npm run test:browser
npm audit --omit=dev --audit-level=moderate
npm audit --audit-level=moderate
cd ../..
```

On Linux, `npx playwright install --with-deps chromium` also installs the required system libraries, as in CI. The required dependency gate audits the production tree; CI retains a full-tree report as well. Audits depend on the locked versions and advisory data available when run.

The browser regression scripts use the built application with generated media and synthetic API responses. They exercise browser behavior, including publication request/status handling. Real API, worker and destination behavior requires the separate checks below.

## API and migration checks

The [CI API job](../.github/workflows/ci.yml) provisions disposable PostgreSQL and Redis services. To reproduce it locally, first provide the fresh services/databases described above and activate a Python 3.12 environment. The sample URLs below use the CI fixture credentials only.

```sh
python -m pip install -r apps/api/requirements.txt -c apps/api/constraints.txt
export CI_DATABASE_URL='postgresql+psycopg://postgres:postgres@127.0.0.1:5432/semantic_ci'
export CI_MIGRATION_DATABASE_URL='postgresql+psycopg://postgres:postgres@127.0.0.1:5432/semantic_ci_migration'
export CI_REDIS_URL='redis://127.0.0.1:6379/15'
export SEMANTIC_ENV=development
export JWT_SECRET_KEY='ci-synthetic-jwt-secret-at-least-32-bytes'
export ENCRYPTION_MASTER_KEY='ci-synthetic-encryption-key-only'
export OPENAI_API_KEY=''
export SENTRY_DSN=''
export EMBEDDING_ENABLED=false
export EMBEDDING_WARMUP_ENABLED=false
python -m unittest discover -s scripts/ci -p 'test_disposable_targets.py' -v
python -m unittest discover -s scripts/ci -p 'test_publication*.py' -v
python -m unittest discover -s scripts/ops -p 'test_*.py' -v
./scripts/ci/rehearse-migrations.sh
python scripts/ci/run-api-suite.py
```

The migration rehearsal upgrades an empty disposable database, downgrades it to base, and upgrades it again. It is destructive to that test schema. The API runner executes every test module separately, writes JUnit reports and `api-suite-summary.json` under `/tmp/loci-api-ci` (or `CI_ARTIFACT_DIR`), and fails on any skipped case or missing required projection-privacy check.

Some tests substitute selection or transport boundaries. Read their stated scope: PostgreSQL/Redis worker-boundary checks and helper guards do not establish actual production-worker, SSH, destination or public HTTP acceptance. The [durable-publication guide](durable-publication.md#upgrade-rollback-and-acceptance) defines the coordinated upgrade, recovery and acceptance requirements for that optional integration.

## Running-demo checks

Start a fresh generated demo with the [quickstart](../README.md#quickstart), then run:

```sh
python3 scripts/demo/verify.py
cd apps/web
npm run test:demo
cd ../..
```

These checks use the actual seeded API, database and public reader for model/media delivery, ranged video, annotation/clip playback, share links and citations. For the visual creation → private preview → annotation publication journey, enable the optional console using the [authoring guide](demo-authoring.md), then run `npm run test:demo:authoring` from `apps/web`. That check creates and removes a generated annotation; use the disposable demo only.

The clean-demo CI job uses the Linux-only [demo override](../docker-compose.demo-ci.yml) with a 256 MiB in-memory media volume. It generates samples inside the running API container, captures redacted diagnostics, then removes its disposable project volumes. The local quickstart uses persistent storage.

## Interpreting results

Keep the exact candidate SHA with logs, failures, skips and test artifacts. Passing synthetic checks does not establish larger-collection performance, other browsers/devices, optional external integrations, research-content permissions or hosted-deployment readiness. Supported institutional private-to-public deployment still requires combined worker/restart, authorization/privacy, media/citation readback and rollback acceptance; a build or contract test alone cannot supply that evidence.

The PostgreSQL checks cover empty-database migration upgrade/downgrade/re-upgrade and refusal to downgrade populated publication history. Application/code rollback with retained history remains unvalidated, including older API and worker versions reading new jobs, generations and media. Compatibility with external consumers of legacy `PublicationManifest` current/projected pointers is also unproved.
