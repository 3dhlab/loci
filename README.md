# Loci

Loci connects points on 3D models to video evidence, timed transcripts, citations and shareable moments.

**[Quickstart](#quickstart)** · **[Developer docs](docs/README.md)** · **[Hosted walkthrough](#hosted-research-instance)** · **[Local demo video](#local-demo-walkthrough)** · **[Changelog](docs/CHANGELOG.md)**

## Quickstart

Use Docker Engine with Compose v2 or Docker Desktop, Python 3, internet access, and free ports 8000 and 8080. Clone the repository or open the root of a [release archive](https://github.com/3dhlab/loci/releases).

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

From a release archive, start at `python3 scripts/demo/init-env.py`. Use `docker-compose` if that is your Compose v2 command.

Open [localhost:8080/public](http://localhost:8080/public), select **Demo cube**, and choose an annotation. The [demo guide](docs/demo.md) covers setup details, upgrades, API authoring and replacing the sample content. The [authoring guide](docs/demo-authoring.md) enables the optional visual console.

## Documentation

- [Architecture](docs/architecture.md) · [API](apps/api/README.md) · [Web](apps/web/README.md)
- [Development checks](docs/validation.md) · [Contributing](.github/CONTRIBUTING.md) · [Support](.github/SUPPORT.md) · [Security](.github/SECURITY.md)

## Hosted research instance

[Explore the research collection](https://loci.threedeezy.com/public) or watch the 50-second walkthrough with audio.

https://github.com/user-attachments/assets/9334c52c-25dc-4af4-ae11-c206da51f4df

[Walkthrough guide](docs/media/hosted-walkthrough.md) · [Download the MP4](https://github.com/3dhlab/loci/raw/refs/heads/main/docs/media/hosted-walkthrough.mp4)

Recording and editing by **Unlock Digital**. The walkthrough has separate presentation permissions and grants no reuse license for the video, previews or depicted research content. [Media permissions and credits](docs/media/README.md)

## Local demo walkthrough

This silent 41-second walkthrough shows how to place a pin, preview an annotation privately, publish it, and check it in the reader.

https://github.com/user-attachments/assets/daaeb92e-b3a2-4adc-af80-01662bce84bd

[Timed text description](docs/media/demo-authoring/transcript.md) · [Download the MP4](docs/media/demo-authoring/loci-demo-authoring.mp4)

Demo improvements and video by the **3D Humanities Lab**. [Provenance and license](docs/media/demo-authoring/README.md)

## Project and license

Maintained by Craig Stevens. [Cite Loci](CITATION.cff) · [Report an issue](https://github.com/3dhlab/loci/issues) · [Apache 2.0 license](LICENSE) · [Licensing scope and attribution](docs/licensing/scope.md)
