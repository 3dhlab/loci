# Contributing

Use GitHub Issues for reproducible bugs and scoped feature proposals. Review existing issues first. Describe your expected behavior, current behavior, version, environment and reproduction using generated sample content. Follow SECURITY.md for private vulnerability reporting.

Keep pull requests focused on one change. Explain the problem, resulting behavior, validation and migration/configuration effects. Update documentation when setup or interfaces change. Follow the existing JavaScript module style (two spaces, single quotes) and Python PEP 8 conventions. Prefer named functions and meaningful domain names; comments should explain important assumptions or design decisions.

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
