# Loci

Loci connects points on 3D models to video evidence, timed transcripts, citations and shareable moments. Explore an object, select an annotation, and watch the interpretation linked to that place.

## Hosted research instance

[Explore the Loci research collection](https://loci.threedeezy.com/public), or watch this 50-second walkthrough with audio:

https://github.com/user-attachments/assets/9334c52c-25dc-4af4-ae11-c206da51f4df

[Walkthrough guide](docs/media/hosted-walkthrough.md) · [Download the MP4](https://github.com/3dhlab/loci/raw/refs/heads/main/docs/media/hosted-walkthrough.mp4)

Recording and editing: **Unlock Digital**. The hosted collection has separate content permissions. Permission to show participants in this walkthrough grants no reuse license for its video, previews or depicted research content. See [media permissions and credits](docs/media/README.md).

## Local demo walkthrough

The included demo uses a generated cube, video and transcript. This silent 41-second walkthrough shows how to place a model pin, preview its video range privately, publish the annotation and check it in the reader.

https://github.com/user-attachments/assets/daaeb92e-b3a2-4adc-af80-01662bce84bd

[Authoring guide](docs/demo-authoring.md) · [Timed text description](docs/media/demo-authoring/transcript.md) · [Download the MP4](docs/media/demo-authoring/loci-demo-authoring.mp4)

Demo improvements and video: **3D Humanities Lab**. These generated assets are Apache 2.0; see [provenance and license](docs/media/demo-authoring/README.md).

## Quickstart

Requires Docker Engine with Compose v2 or Docker Desktop, Python 3, internet access, and free ports 8000 and 8080. Containers run Linux. Clone the source, or extract a [release archive](https://github.com/3dhlab/loci/releases) and enter its root folder:

```sh
git clone https://github.com/3dhlab/loci.git
cd loci
python3 scripts/demo/init-env.py
docker compose build
docker compose up -d postgres redis
docker compose run --rm api alembic upgrade head
docker compose run --rm api python -m app.scripts.seed_demo
docker compose up -d --wait
python3 scripts/demo/verify.py
```

For an extracted archive, skip the first two commands. If Compose v2 is installed as `docker-compose`, use that command instead.

Open [localhost:8080/public](http://localhost:8080/public), select **Demo cube**, then an annotation. Transcript lines seek the video; citations and share links return to the selected evidence. The [authoring guide](docs/demo-authoring.md) enables the optional visual console. API forms are at [localhost:8000/docs](http://localhost:8000/docs).

The demo account is `demo@example.org`; find its generated `DEMO_PASSWORD` in your private `.env`. Keep that file with the database volume. The website and API bind to localhost, and setup preserves existing credentials.

`docker compose down` stops the demo and preserves data. Adding `--volumes` deletes that Compose project's database and media.

## Documentation

- [Documentation index](docs/README.md): setup, authoring, architecture and operations.
- [Use your own content or upgrade the demo](examples/README.md).
- [Development checks](docs/validation.md) and [contributing](.github/CONTRIBUTING.md).
- [Support](.github/SUPPORT.md), [security reporting](.github/SECURITY.md) and [release notes](CHANGELOG.md).

The quickstart covers a local demo with one authoring account. Remote publication and other integrations need separate setup. The durable-publication changes on this development branch remain under review; see their [upgrade and acceptance requirements](docs/durable-publication.md#upgrade-rollback-and-acceptance).

## Project and license

Maintained by Craig Stevens. Report ordinary bugs through [GitHub Issues](https://github.com/3dhlab/loci/issues) and cite the software using [CITATION.cff](CITATION.cff). Contributions follow the [code of conduct](.github/CODE_OF_CONDUCT.md).

First-party software, documentation and generated samples are [Apache 2.0](LICENSE). Third-party dependencies and research media have separate terms. See [license scope and media exceptions](docs/licensing/scope.md), [dependency notices](docs/licensing/README.md) and [NOTICE](NOTICE).
