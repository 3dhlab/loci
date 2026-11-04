# Loci documentation

Start with the [quickstart](../README.md#quickstart) for a local demonstration using generated content. The demo has one authoring account and a public reader for published records. It does not configure a hosted research collection or optional external services.

## Use Loci

- [Generated sample and content workflow](../examples/README.md): installation, upgrades, API authoring, sample refresh and replacing the model or video.
- [Visual authoring](demo-authoring.md): select a video range, place a pin, preview privately and publish an annotation.
- [Viewer and playback behavior](viewer-camera-continuity.md): camera continuity, annotation selection and bounded playback.
- [Public theme configuration](public-theme-configuration.md): supported appearance settings.
- [Hosted walkthrough](media/hosted-walkthrough.md) and [local-demo timed description](media/demo-authoring/transcript.md).

## Develop and operate

- [Validation](validation.md): prerequisites, commands and limits of the automated checks.
- [Contributing](../.github/CONTRIBUTING.md): pull requests, required CI and maintainer review/release rules.
- [Architecture](architecture.md) and [data contracts](data-contract.md).
- [API](../apps/api/README.md) and [web](../apps/web/README.md) development notes.
- [Operations](operations.md): public-reader probes and filesystem health.
- [Durable publication](durable-publication.md) and [readback](publication-readback.md): development-stage protocols, coordinated upgrades, recovery and retained-history rollback. Full transport and recovery acceptance remains required.

The frontend and 3D download remain large. On mobile and limited-resource devices, readers choose when to load a model. Automated browser checks use Chromium; other browsers, larger collections and hosted deployments need their own validation. Email, transcription and semantic indexing require additional configuration.

## Help, rights and releases

- [Support](../.github/SUPPORT.md), [security reporting](../.github/SECURITY.md) and [community conduct](../.github/CODE_OF_CONDUCT.md).
- [Release notes](../CHANGELOG.md) and [software citation](../CITATION.cff).
- [License scope](licensing/scope.md), [sample assets](licensing/samples.md), [dependency notices](licensing/README.md) and [media permissions and attribution](media/README.md).
