# Unlock Digital assessment of corrected Lab draft #7

Assessment date: October 5, 2026, America/Chicago. Unlock Digital performed the source review and local validation through Codex in an isolated checkout.

## Decision and assessed source

**Adopt the corrected draft for the documented generated-demo and single-range authoring workflow, subject to the Lab owner's review and the submitted head's four required CI checks.** The bounded review found no substantiated blocking regression. Confidence is high for that tested workflow; hosted operation and broader authoring capabilities retain their separate verification scope.

The assessed source is [Lab draft #7](https://github.com/3dhlab/loci/pull/7) at **699b41f3855ceccf366273da686b4909cb2c04e9**, based on **71bfb23c5dc6cfb4c7b724ae7829c56fff928600**. The adoption branch starts at that exact commit. Its ten original commits, author/committer identities and SHA values remain intact. Unlock Digital adds this assessment as a separate documentation commit. Application, scripts, CI, dependency and tutorial files retain the assessed Lab bytes.

The later Lab commit `90a03f14a60fdab31e1bd1d86d97cdf1fcf82b00` prepares the credited release changelog. This submission follows the explicitly requested corrected source revision. The Lab can continue preparing that release material while this submission is pending.

## Purpose and operating boundaries

The change makes the generated sample's annotation-to-video relationship visible through chapter text, square/triangle/circle symbols, a moving feature marker and an absolute video clock. The optional console maps one video range to a captured model point, previews the private annotation and publishes that annotation individually. Direct reader pin selection keeps the manually chosen model view while its linked evidence plays.

The supported local installation uses the existing web, API, PostgreSQL and Redis services. The console Compose override changes the web build surface and retains the API, volumes and network configuration. Generated media stays in the installation's media storage; transcript and publication records stay in its database. The change adds no schema migration or new publication privilege. Optional remote integrations retain their existing configuration boundaries.

## Critical findings

| Area | Assessment and supporting evidence |
| --- | --- |
| Transcript refresh and conflict handling | `reconcileTranscriptDraft` tracks viewer/session identity and the loaded source text. Clean drafts follow fetched text. Unsaved same-source text survives workspace navigation. A changed or unavailable source preserves the draft and disables Save; confirmed reconciliation replaces both text and undo history. Source unit cases and the synthetic console journey cover ingestion, conflict, cancellation, confirmed loading, missing/recreated transcripts and unchanged-source recovery. This protects the observed refresh conflict; concurrent server writes between a fetch and Save remain part of the existing API editing contract. |
| Placement and individual publication | Blank coordinates are rejected while numeric zero remains valid. Surface capture retains the mapped range. The individual publication action updates the selected annotation through the existing ownership-checked API. The real-demo evidence confirms private projection exclusion, private preview, individual publication, bounded reader playback and deletion restoring the original annotations. |
| Reader camera and guided playback | Camera preservation is scoped to direct model selection. Rail and restored-link navigation retain deliberate framing. Decoded-frame boundary handling pauses inside an exclusive single-range end; manual scrubbing disarms guided focus. The source camera report records maximum direct-selection pose change of `0` with reduced motion and approximately `0.0005` with ordinary motion, within the existing `0.005` tolerance. |
| Synthetic refresh and rollback | The explicit development-only refresh verifies the incoming manifest, media checksum, duration, codec, dimensions, seed identities, selected geometry, segment text and timing, and annotation playlists. It generates media before switching database references and retains old files. Tests exercise personalized-segment and custom-point rejection, partial-output cleanup and transaction failure. Operators should use it for the documented synthetic sample, inspect their installed content and retain a backup; its selected seed checks provide a bounded eligibility guard. |
| Authenticated playback and privacy | The stream endpoint follows the persisted playback rendition while retaining the project-owner query and READY-state check. Invalid rendition identity returns 404. The web cache revision follows the video's update value. Existing public/private projection checks execute against PostgreSQL in the source CI run. |
| Dependencies and provenance | The reviewed lockfile pins brace-expansion `5.0.12`; Python requirements and constraints agree on PyJWT `2.15.0`. Associated notices and inventories are updated. The independent npm audit reports zero vulnerabilities at assessment time. The generated sample and Lab tutorial remain first-party documentation with the existing Apache 2.0 licensing scope. |
| Setup, compatibility and scope | The opt-in console retains the existing demo API and volumes. The guide distinguishes individual annotation publication from package publication and documents restoration of the public-only build. General multi-range publication requires completed public clip identities and an export workflow; new collection objects require supported identity/readiness provisioning. These limits are stated in the authoring guide. |

The locked install emits the existing `three-mesh-bvh@0.7.8` deprecation warning. The reviewed dependency audit and browser checks pass; dependency compatibility remains a maintenance follow-up. The large existing application and style modules continue to increase review effort. Those follow-ups belong in future scoped changes.

## Independent validation

Local checks used Node **22.12.0**, locked dependencies with lifecycle scripts disabled, and a fresh source checkout:

- `npm test`: **138 passed, zero failed or skipped**.
- Production builds for `VITE_APP_SURFACE=public` and `VITE_APP_SURFACE=console`: **passed**.
- Full `npm audit --json`: **zero reported vulnerabilities**.
- `python3 scripts/ci/check-public-fixtures.py`: **passed**.
- Standard-library/FFmpeg sample regeneration: **passed**; inspected the three encoded chapter frames and decoded the complete 960×540, 24 fps, twelve-second video.
- Tutorial probe: H.264, **1920×1080, 30 fps, 40.7 seconds**, silent. Its provenance explains the opening overview's earlier Top edge position and links the current refresh correction.

[Source CI run 37320689837](https://github.com/3dhlab/loci/actions/runs/37320689837) identifies head `699b41f3855ceccf366273da686b4909cb2c04e9` and passes all four required jobs. Its PR merge candidate is `2824f8c6c7478ecdcc99918021231695189bf3d0`. The assessment inspected the job steps, API JUnit evidence, migration logs, real-demo authoring report and camera continuity report:

- **624 API tests across 31 JUnit modules**, zero failures, errors or skips; PostgreSQL public-projection privacy and upgrade/downgrade/re-upgrade verification pass.
- Real-demo private creation, preview, publication and cleanup pass. The new point is on the cube surface; single-range playback stops at **3.958333 seconds** inside its four-second window.
- Reduced-motion and ordinary-motion camera cases preserve direct selection, retain deliberate rail framing and restore annotation links.

These backend and browser results are inspected GitHub CI evidence. Local validation covers the checks listed separately above. Fresh CI on the independent submission remains required before merge.

## Release preparation handoff

1. The Lab can prepare credited release notes, setup instructions, archive checks and the publication date while the fork submission awaits review. Its release metadata should identify the actual final reviewed main commit.
2. The Lab owner critically reviews this submission, resolves discussion and verifies all four required checks on its final head through the existing [contribution process](../../CONTRIBUTING.md).
3. After acceptance and merge, update [hosted walkthrough PR #6](https://github.com/3dhlab/loci/pull/6) from the actual corrected main baseline, inspect its media permissions and README placement, and re-review its final head.
4. Complete the credited changelog through the protected contribution path. Verify CI and the supported demo on the final main commit before the owner-controlled tag and GitHub release.

The existing environment configuration and database/media volumes should be retained during image rebuilds. The documented explicit synthetic refresh and project backup provide the sample-upgrade rollback path. General authoring, hosted operations, collection-scale performance and broader device coverage remain separate work.

All installation artifacts, generated review media and raw CI evidence from this assessment stay outside the submitted source tree. The fork submission preserves Lab authorship and leaves the acceptance, merge and release decisions with the Lab owner.
