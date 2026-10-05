# Loci

Loci connects locations on a 3D model to video evidence, timed transcripts, citations, and shareable moments. Researchers can select a point on an object, watch the related video section, read its transcript and share the evidence with others.

The source includes a local demonstration with one authoring account; [published releases](https://github.com/3dhlab/loci/releases) identify the available packages. The sample model, video, transcript, and annotations are created specifically for the demonstration. Loci's original software, documentation and generated samples are licensed under [Apache 2.0](LICENSE); [licensing details](LICENSE_STATUS.md) explain the scope and third-party terms.

## Hosted research instance

[Explore the Loci research collection](https://loci.threedeezy.com/public). This personally hosted instance shows the interface with collection objects and recorded interpretations. Its collection media and contributor material have separate permissions. The generated local demonstration below is the reproducible example included with this repository.

Watch the 64.5-second hosted walkthrough with audio.

https://github.com/user-attachments/assets/8ed45214-8053-4f76-a1cf-8d37fdcebe1a

[Read the walkthrough guide](docs/media/hosted-walkthrough.md)

Hosted walkthrough recording and editing: **Unlock Digital**. Permission to
show participants in this edited video grants no reuse license for the
walkthrough, its previews or the depicted research content. See
[media permissions and attribution](docs/media/README.md),
[Apache 2.0 scope and media exceptions](LICENSE_STATUS.md) and [NOTICE](NOTICE).

## Quickstart

Requirements: Docker Engine with Compose v2 or later, or Docker Desktop; Python 3; and internet access for dependencies and images. Containers run Linux. Application ports 8000 and 8080 must be free.

```sh
python3 scripts/demo/init-env.py
docker compose build
docker compose up -d postgres redis
docker compose run --rm api alembic upgrade head
docker compose run --rm api python -m app.scripts.seed_demo
docker compose up -d --wait
python3 scripts/demo/verify.py
```

If your installation provides the standalone `docker-compose` command, substitute it for `docker compose` throughout.

Open [the object browser](http://localhost:8080/public). Select **Demo cube**, then an annotation to play its linked video section. **Compare two sections** plays two ordered windows. Select a transcript line to seek, and copy a share link to return to the selected annotation or clip. Video-focused links include their playback timestamp. Interactive API documentation and authoring forms are available at [localhost:8000/docs](http://localhost:8000/docs).

The demo login is `demo@example.org`. Read its generated `DEMO_PASSWORD` from your local `.env`; keep that file private. The setup script preserves existing credentials. PostgreSQL and Redis communicate within the demo network. The website and API are accessible from your own computer. Email, remote publishing, automatic transcription and semantic indexing are optional integrations with separate setup requirements.

`docker compose down` stops services and preserves data. `docker compose down --volumes` deletes the demo database and media for that Compose project. Keep `.env` for the lifetime of its database volume.

## Local demo walkthrough

https://github.com/user-attachments/assets/daaeb92e-b3a2-4adc-af80-01662bce84bd

[Follow the authoring guide](docs/demo-authoring.md) · [Timed text description](docs/media/demo-authoring/transcript.md)

This silent 41-second walkthrough shows the generated cube in the public reader,
then selects a video range, places and refines a pin, previews it privately,
publishes that annotation, and verifies the result in the reader. The demo code
improvements and this video are a **3D Humanities Lab** contribution. The
[video provenance and license](docs/media/demo-authoring/README.md) describe its
generated content.

Build a source checkout or release archive containing `docker-compose.authoring.yml`
to follow the optional visual authoring walkthrough. The v0.1.1 release predates
that override and the updated chapter media.

## Documentation

- [Synthetic demo and content workflow](examples/README.md)
- [Optional visual authoring console and single-range walkthrough](docs/demo-authoring.md)
- [Spatial selection, camera continuity and playback verification](docs/viewer-camera-continuity.md)
- [Architecture and trust boundaries](docs/architecture.md)
- [Model, annotation, transcript and media contracts](docs/data-contract.md)
- [Development and contribution checks](CONTRIBUTING.md)
- [Security reporting](SECURITY.md) and [support scope](SUPPORT.md)
- [Release notes](CHANGELOG.md), [licensing details](LICENSE_STATUS.md), and [third-party notices](THIRD_PARTY_NOTICES.md)

## What you can do

You can rotate and zoom the model, select annotations, play linked clips, seek through transcript lines, and copy citations and share links. On mobile devices and devices with limited resources, selecting the model-load button starts the 3D view. This gives the reader control over a potentially large download.

The demonstration provides one account for creating and reviewing content. Readers see published records through the public viewer. Hosting multiple authors, managing participant consent, automatic transcription, DOI registration and adaptive streaming are areas for further development or integration. The quickstart covers the local demonstration described here.

Some frontend components remain large, and the 3D viewer produces a large JavaScript download. Improving those components and measuring loading performance are ongoing maintenance priorities. Automated browser checks use Chromium; broader browser and device coverage will require further testing.

## About the project

The public source repository is [3dhlab/loci](https://github.com/3dhlab/loci), maintained by Craig Stevens. Use [GitHub Issues](https://github.com/3dhlab/loci/issues) for support and ordinary bug reports. Send private security or conduct reports to [craig.stevens@austin.utexas.edu](mailto:craig.stevens@austin.utexas.edu), or use [GitHub's private vulnerability reporting](https://github.com/3dhlab/loci/security/advisories/new) for security reports. Use [CITATION.cff](CITATION.cff) when citing the software. Research collections and participant records require their own permissions and governance. The included examples are generated for testing and demonstration.
