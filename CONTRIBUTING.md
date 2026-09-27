# Contributing

Use GitHub Issues for reproducible bugs and scoped feature proposals. Review existing issues first. Describe your expected behavior, current behavior, version, environment and synthetic reproduction. Follow SECURITY.md for private vulnerability reporting.

Keep pull requests focused on one behavior or feature boundary. Explain the problem, resulting behavior, validation and migration/configuration effects. Update documentation when setup or interfaces change. Follow the existing JavaScript module style (two spaces, single quotes) and Python PEP 8 conventions. Prefer named functions and meaningful domain names; comments should explain invariants or design decisions.

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

The browser regression serves synthetic assets and mocks the public response contract. Actual database/API behavior is tested separately. The local demo check `python3 scripts/demo/verify.py` exercises the supported runtime. CI provides the exact disposable PostgreSQL/Redis environment for the API suite, migration cycle and privacy test; run `python scripts/ci/run-api-suite.py` only with explicit disposable target variables documented in the workflow. It permits no skipped cases.

Tests must use synthetic data and disposable services. Keep participant content, private environment files, credentials, runtime volumes and operational captures out of contributions. Preserve regression coverage when refactoring feature modules. Review migration graphs and rehearse upgrade/downgrade/re-upgrade on empty disposable databases. Schema migrations should establish schema; optional demo content belongs in explicit seed commands.

Maintainers review correctness, supported setup, public/private projection, data-contract compatibility and dependency/content terms before merging. Follow the [conduct policy](CODE_OF_CONDUCT.md).
