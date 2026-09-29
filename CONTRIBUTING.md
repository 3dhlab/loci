# Contributing

Use GitHub Issues for reproducible bugs and scoped feature proposals. Review existing issues first. Describe your expected behavior, current behavior, version, environment and reproduction using generated sample content. Follow SECURITY.md for private vulnerability reporting.

Keep pull requests focused on one change. Explain the problem, resulting behavior, validation and migration/configuration effects. Update documentation when setup or interfaces change. Follow the existing JavaScript module style (two spaces, single quotes) and Python PEP 8 conventions. Prefer named functions and meaningful domain names; comments should explain important assumptions or design decisions.

## Pull request path

Create a branch in your fork, make a focused change, and open a pull request against `main`. Link the relevant issue when there is one. Use the pull request template to describe the change and the checks you ran. Contributors do not need write access to this repository to propose changes.

`main` is maintained through pull requests. A maintainer reviews the diff, confirms that discussions are resolved and the required CI checks pass, then merges the pull request. The pull request author cannot supply the required approval. A later code change needs a fresh approval. Do not push directly to `main`, force-push it, delete it, or use an administrator bypass for an ordinary change.

The required CI jobs are **Web unit, build, dependency gate**, **API PostgreSQL, Redis, migrations**, **Browser annotation, media, share regression**, and **Clean demo install and real API browser journey**. They run on pull requests through [the CI workflow](.github/workflows/ci.yml). A failed or missing required check must be resolved before merging.

The `3dhlab` account owns the repository rules and performs merges. Changes to branch rules, required checks, repository permissions, or this process are owner decisions; record the reason and resulting settings in a pull request or issue. If `3dhlab` authors a pull request, another trusted maintainer with write access must review it before merge. The owner must not approve its own pull request or bypass review to merge it.

Only the `3dhlab` account publishes release tags and GitHub releases, using a reviewed commit already merged to `main` with passing CI. GitHub grants release creation to accounts with repository write access, so ordinary contributors should use forks without write access. Grant write access only to a trusted maintainer when its merge and release authority is intended.

## Checks

Use Node 22 and the npm lockfile. From `apps/web`:

```sh
npm ci --ignore-scripts --no-audit
npm test
npm run build -- --outDir dist-ci
npx playwright install chromium
npm run test:browser
npm audit --audit-level=moderate
```

The browser regression uses generated assets and sample API responses. Separate tests exercise the real API and database. The local demo check `python3 scripts/demo/verify.py` exercises the supported runtime. GitHub Actions provides temporary PostgreSQL and Redis services for API, migration and privacy checks; run `python scripts/ci/run-api-suite.py` only with explicit disposable target variables documented in the workflow. Every backend test must run.

Tests must use generated data and temporary services. Keep participant content, private environment files, credentials, runtime volumes and operational captures out of contributions. Keep the existing behavior checks when reorganizing code. Review migration graphs and rehearse upgrade/downgrade/re-upgrade on empty disposable databases. Schema migrations should establish schema; optional demo content belongs in explicit seed commands.

Maintainers review correctness, supported setup, the separation of public and private information, compatibility of data formats, and the terms for dependencies and content before merging. Follow the [conduct policy](CODE_OF_CONDUCT.md).
