# Loci

Loci connects locations on a 3D model to video evidence, timed transcripts, citations, and shareable moments. It gives researchers and developers a self-hostable example of GLB → spatial annotation → linked video segment → shared evidence.

This v0.1 candidate supports a local, single-operator demonstration. The sample model, video, transcript, and annotations are generated from numerical geometry and synthetic text. The repository's software license and sample redistribution terms are pending; see [license status](LICENSE_STATUS.md) before reuse.

## Quickstart

Requirements: Docker Engine with Compose v2 or later, or Docker Desktop; Python 3; and internet access for dependencies and images. Containers run Linux. Application ports 8000 and 8080 must be free.

```sh
python3 scripts/demo/init-env.py
docker compose build
docker compose up -d postgres redis
docker compose run --rm api alembic upgrade head
docker compose run --rm api python -m app.scripts.seed_demo
docker compose up -d
python3 scripts/demo/verify.py
```

If your installation provides the standalone `docker-compose` command, substitute it for `docker compose` throughout.

Open [the object browser](http://localhost:8080/public). Select **Demo cube**, then an annotation to play its linked video section. **Compare two sections** plays two ordered windows. Select a transcript line to seek, and copy a share link to return to the selected annotation or clip. Video-focused links include their playback timestamp. The API schemas and interactive authoring forms are at [localhost:8000/docs](http://localhost:8000/docs).

The demo login is `demo@example.org`. Read its generated `DEMO_PASSWORD` from your local `.env`; keep that file private. The initializer refuses to overwrite existing credentials. PostgreSQL and Redis remain internal to the demo network, and public application ports bind to loopback. The demo starts no email, remote publication, transcription, or embedding-download service.

`docker compose down` stops services and preserves data. `docker compose down --volumes` deletes the demo database and media for that Compose project. Keep `.env` for the lifetime of its database volume.

## Documentation

- [Synthetic demo and content workflow](examples/README.md)
- [Architecture and trust boundaries](docs/architecture.md)
- [Model, annotation, transcript and media contracts](docs/data-contract.md)
- [Development and contribution checks](CONTRIBUTING.md)
- [Security reporting](SECURITY.md) and [support scope](SUPPORT.md)
- [Release notes](CHANGELOG.md), [license status](LICENSE_STATUS.md), and [third-party notices](THIRD_PARTY_NOTICES.md)

## Supported scope and limits

The viewer supports orbit/zoom, spatial annotations, ordered linked clips, timed transcript seeking, citations, posters, deep links, responsive controls, and recovery states. Mobile/constrained devices require an explicit model-load action to bound memory and transfer costs.

The local demo provides one authenticated authoring account and a public read-only projection. Remote publication, hosted multi-user authoring, consent workflows, automatic transcription, DOI registration, adaptive streaming and broad scale claims require additional configuration or future implementation. The backend retains modules for optional integrations; the quickstart configures only the documented local viewer path.

The frontend contains large legacy feature components and the 3D bundle exceeds Vite's default chunk-size advisory. Gradual feature extraction and delivery profiling remain maintenance priorities. Chromium synthetic checks provide automated coverage; supported device and browser claims should follow explicit release validation.

## Project

The planned public repository is [3dhlab/loci](https://github.com/3dhlab/loci). Support and contribution routes become active when the reviewed candidate is published. Author: Craig Stevens. Private security/conduct reports: craig.stevens@austin.utexas.edu. Software citation metadata is in [CITATION.cff](CITATION.cff). Research collections and participant records require their own rights and governance; this package contains synthetic examples.
