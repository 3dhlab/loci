# LOCI API

FastAPI provides authenticated authoring, public collection and evidence projections,
media delivery, and optional background processing. PostgreSQL stores metadata;
media files live under `MEDIA_ROOT`. Redis supports rate limits and optional jobs.

See [the demo guide](../../examples/README.md) for the supported local setup and
[architecture](../../docs/architecture.md) for component responsibilities. Once
running, `/docs` exposes the API schemas and request forms.

## Commands

Run these inside the API container (or a Python 3.12 environment with dependencies
from `requirements.txt` and an explicit disposable `DATABASE_URL`):

```sh
alembic upgrade head
python -m app.scripts.seed_demo
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Demo seeding is explicit and requires an empty development database. Schema
migrations create no operator accounts or subscribers. Shared, staging and
production service startup requires generated signing, encryption and agent
secrets. The local demo generates its own credentials in an ignored `.env` file.

Run tests using the commands in [CONTRIBUTING](../../CONTRIBUTING.md). Database
integration tests require a disposable PostgreSQL instance with pgvector.
