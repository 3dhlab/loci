# Vite denied-file bypass security maintenance — October 26, 2026

Base: canonical `3dhlab/loci` main at `f545f45b0a0629aa7e1a24dc564dbea71fcaef27`, rechecked before final review. Branch: `security/vite-6.4.5`.

[GHSA-8g5g-mxqh-grgj](https://github.com/vitejs/vite/security/advisories/GHSA-8g5g-mxqh-grgj) identifies Vite 6.4.4 as affected and 6.4.5 as patched. The change raises the manifest minimum from `^6.4.4` to `^6.4.5` and resolves exactly 6.4.5 in the npm lockfile. No other locked package changes. The repository has one npm lockfile and one Vite resolution; `npm ls vite --all` confirms the React plugin uses that same resolution. Vite's license text is byte-identical; existing notices and the license inventory receive version-only updates.

## Safe reproduction

`node --test tests/vite-deny.test.mjs` runs from `apps/web`. The test creates an ephemeral directory with a synthetic denied `.crt` fixture and an allowed text control, disables environment/config loading, restricts the allowed filesystem root, and binds an ephemeral port on `127.0.0.1`. Cleanup closes the server and removes the directory. No private file is used.

| Request to synthetic denied fixture | Vite 6.4.4 control | Vite 6.4.5 |
| --- | --- | --- |
| Plain request | 403; no marker | 403; no marker |
| `?raw` | 403; no marker | 403; no marker |
| `?import&raw` | 200; synthetic marker disclosed | 403; no marker |
| `?import&url&inline` | 200; encoded synthetic marker disclosed | 403; no marker |

The vulnerable control fails the regression as intended. The patched test passes and is included automatically in `npm test`. An allowed-file 200 response guards against a test that merely denies everything.

## Local validation

Node 22.22.0 / npm 10.9.4 on macOS arm64; Python 3.12 for host checks. Containers use the repository Dockerfiles and disposable PostgreSQL/Redis. Commands below are from repository root unless the working directory is specified.

| Command | Result |
| --- | --- |
| `npm ci --ignore-scripts --no-audit` (apps/web) | Passed with the patched lockfile |
| `npm test` (apps/web) | 143 assertions/subtests passed, zero failed/skipped; includes 138 existing tests and the new regression |
| `npm run build -- --outDir dist-ci` (apps/web) | Passed; existing large-chunk warning |
| `npx playwright install chromium` then `npm run test:browser` (apps/web) | All three browser scripts passed: annotation/share, public browse/poster, console surface |
| `npm audit --audit-level=moderate` (apps/web) | Zero vulnerabilities |
| `npm audit --omit=dev --audit-level=moderate` (apps/web) | Zero vulnerabilities |
| `CI_AUDIT_OUTPUT=/private/tmp/loci-vite-security-tools/dependency-audit-full.json node ../../scripts/ci/capture-npm-audit.mjs` (apps/web) | CI audit capture passed; production and full tree both zero |
| `python3.12 scripts/ci/check-public-fixtures.py` | Passed |
| `python3.12 scripts/ci/test_check_public_fixtures.py` | Four tests passed |
| `python3.12 -m unittest discover -s scripts/ci -p 'test_disposable_targets.py' -v` | Seven tests passed |
| `python3.12 -m unittest discover -s scripts/ops -p 'test_*.py' -v` | Seven tests passed |
| `bash scripts/ci/rehearse-migrations.sh` (disposable tests container) | Upgrade/downgrade/re-upgrade passed through `20260813_0036` |
| `python scripts/ci/run-api-suite.py` (disposable tests container) | 629 cases in all 33 modules passed, no skipped cases; real PostgreSQL public-projection privacy assertion passed |
| `python3.12 scripts/demo/verify.py` | Passed against the new synthetic demo |
| `npm run test:demo` (apps/web) | Real-API browser journey and both camera-continuity motion preferences passed |
| `npm run test:demo:authoring` (apps/web) | Create/private preview/publish/cleanup journey passed |
| `node --check apps/web/tests/vite-deny.test.mjs` and `git diff --check` | Passed |

The repository defines no separate lint or type-check command. The prescribed web aggregate's install, unit, build, audit and browser commands were all run. The other three required CI job equivalents were exercised locally with the services described below.

A temporary `node test-results/vite-runtime-smoke.mjs` run from `apps/web` also passed: actual project dev-server startup and homepage response, transformed synthetic module, Chromium-observed HMR update with the same document retained, and production preview HTML plus all three referenced assets. Both servers bound loopback and were closed; the generated source fixture was removed. The script and logs are retained in the maintainer's local evidence bundle, outside the public source tree.

## Disposable container commands and environment adaptations

A temporary Docker CLI/Compose client used the existing local VM without altering its settings or other services. Two unique Compose projects isolated all new containers, networks and volumes. The API test image inherited the freshly built API image and copied only tracked `apps/api` and `scripts` sources from the base commit (unchanged by this patch). This replaced a host bind mount unavailable inside this VM. The test service retained the repository's explicit disposable database URLs and `CI_DISPOSABLE_SERVICES=true`, with no host ports.

Using `demo` as shorthand for `docker-compose -p loci-vite-security-20261026 -f docker-compose.yml -f docker-compose.demo-ci.yml`, the exact service sequence was:

```sh
python3.12 scripts/demo/init-env.py
demo build
demo up -d --wait postgres redis
demo run --rm api alembic upgrade head
demo up -d --wait api
demo exec -T api python -m app.scripts.seed_demo
demo up -d --wait
python3.12 scripts/demo/verify.py
# Run npm run test:demo from apps/web.
docker-compose -p loci-vite-security-20261026 -f docker-compose.yml -f docker-compose.demo-ci.yml -f docker-compose.authoring.yml build web
docker-compose -p loci-vite-security-20261026 -f docker-compose.yml -f docker-compose.demo-ci.yml -f docker-compose.authoring.yml up -d --no-deps --wait web
# Run npm run test:demo:authoring from apps/web.
demo down --volumes
```

For the API project, `docker-compose -p loci-vite-security-api-20261026 -f docker-compose.ci.yml -f /private/tmp/loci-vite-security-tools/api-image.yml` ran `up -d --wait postgres redis`, `exec -T postgres createdb -U postgres semantic_ci_migration`, then `run --name loci-vite-security-migrations-20261026 tests bash scripts/ci/rehearse-migrations.sh` and `run --name loci-vite-security-suite-20261026 tests python scripts/ci/run-api-suite.py`. The temporary override selected `loci-vite-security-20261026-api-tests` and cleared host bind mounts. XML reports were copied out before `down --volumes --remove-orphans`.

Initial harness attempts required correction: macOS's default Python 3.9 was replaced by the prescribed Python 3.12; loopback checks were rerun with sandbox permission; the unavailable VM bind mount was replaced by the tracked-source image. Successful results above are the corrected runs. No application change was made to accommodate these environment differences.

## Exposure and remaining limits

`apps/web/Dockerfile` launches Vite with `--host 0.0.0.0` inside its container. Reachability depends on the caller's port publishing/network configuration. The advisory concerns the development server's denied-file handling. The application Vite configuration defaults to loopback, with an explicit LAN opt-in; neither setting is changed here.

The supported public/demo Compose build uses `Dockerfile.public`: Vite builds static assets in a build stage and Nginx serves the final image. The demo publishes only loopback ports. Source review and the Nginx-served demo tests provide no evidence that the public static server is affected by this Vite development-server bypass. No production deployment, private installation, LAN exposure, Safari/Firefox or external hosting was tested or changed. Hosted CI on the eventual exact commit remains a separate post-push verification; local results do not assert its outcome.

## Rollback

Before merge, close the draft PR and leave main unchanged if the patch must be withdrawn. After an approved merge, a separately reviewed `git revert <security-fix-commit>` followed by `npm ci --ignore-scripts --no-audit` restores the previous dependency tree; it also restores vulnerable Vite 6.4.4. Keep any reverted development server restricted to loopback pending a replacement fix. This patch has no database/schema migration or production configuration change. No deployment or volume operation is needed to withdraw this source-only proposal.
