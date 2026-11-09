# Changelog

## v0.3.1

- Updated Pillow to 13.0.0 and FastEmbed to 0.9.1 for the [TIFF/EXIF](https://github.com/python-pillow/Pillow/security/advisories/GHSA-wqgw-9xfh-qf2g) and [bundled OpenJPEG](https://github.com/python-pillow/Pillow/security/advisories/GHSA-78g4-xqwv-q3g8) advisories. Updated the dependency notices and added generated image compatibility tests. Unlock Digital contributed the patch in [PR #18](https://github.com/3dhlab/loci/pull/18).
- Pinned the browser CI jobs to Ubuntu 24.04 for the Playwright version used in this release.
- Added the [development roadmap](roadmap.md) to the documentation index.

To upgrade from v0.3.0, back up the database and media, retain `.env` and existing volumes, and rebuild and recreate the API container from v0.3.1. The release adds no schema migration. The supported Docker build uses Python 3.12 and installs the published Pillow wheel containing the OpenJPEG fix. An install without a compatible wheel fails. For a custom source build, verify the linked OpenJPEG fix separately. See the [upgrade guide](demo.md#upgrade-an-existing-installation).

## v0.3.0

- Updated Vite to 6.4.5 for the development-server denied-file query bypass, and scoped Sentry's file-matching dependencies to glob 14.0.2 and minimatch 11.0.1. Matching lockfile and license records are included.
- Added PostgreSQL publication admission and retained operation/generation history, frozen selections, a PostgreSQL outbox with Redis delivery and worker claims. The protocol checks current authority before effects and retains uncertain outcomes for read-only reconciliation or explicit cancellation. Ordinary active-user publication permissions remain in place.
- Added per-account/API browser request recovery after lost acknowledgements, exact-operation status reads, and stale-response and busy-state protection. Private preview visibility, queued work, uncertainty and verified publication have distinct interface states.
- Added frozen public citation-attribution and media identities, generation-aware video playback, and required destination/HTTP readback before the protocol reports publication success. Repaired transcript child re-ingest ordering, standalone model registration, target-limit recovery, reconciliation leases and retry cooldowns.
- Restored inline hosted-walkthrough playback and its attachment provenance. Reorganized installation, validation, community and licensing documentation while preserving both walkthroughs, generated-sample terms, third-party notices and research-media permissions.

Remote sync admission now returns **202 queued** and requires an `Idempotency-Key`, including on the older `/sync` URL. Source API, publication worker and destination need compatible code and migration `20261103_0037`. Back up the database and media, preserve `.env` and volumes, rebuild the applicable images, and follow the [upgrade and rollback guide](durable-publication.md#upgrade-rollback-and-acceptance). Publication history requires retaining the additive schema and receipts during code rollback; a populated history cannot be downgraded away. Legacy `PublicationManifest` current/projected pointers are not updated by the new ledger.

Validation covers the generated local demo and single-range visual authoring, web/browser contract checks, and real PostgreSQL/Redis component cases. Production worker CLI/fork lifecycle, real two-sided SSH and multiple-generation HTTP delivery, combined browser recovery, and larger-collection stress acceptance remain undemonstrated. Empty-database migration cycles and refusal to downgrade populated publication history are tested. Application/code rollback with retained history remains unvalidated, including older API/worker versions reading new jobs, generations and media. Compatibility with external consumers of legacy `PublicationManifest` current/projected pointers is also unproved. Turnkey institutional private-to-public deployment is a next-release goal. This release documents the implemented protocols and their limits; it does not update a hosted installation. See the [validation guide](validation.md) for reproducible checks and their boundaries.

## v0.2.1

- Repaired the public search response wrapper and made loading, error, empty, and results states exclusive. Added brief collection and 3D viewer guidance.
- Added configurable public sitemap generation for the homepage and object browser, with a matching sitemap entry in `robots.txt`.
- Added runtime filesystem capacity reporting and a privacy-limited public reader probe. See the [operations guide](operations.md).
- Pinned the transitive `source-map-js` build dependency to patched version 1.2.2.
- Updated Vite to 6.4.4 with matching lockfile and dependency notices; corrected public search regression assertions and health-route authentication guidance.
- Refreshed Unlock Digital's hosted walkthrough to the approved 50.1-second cut, with clearer captions, pacing, citation steps and closing invitation. Its presentation permission and no-reuse terms remain unchanged.

Unlock Digital contributed the sustained-search and reader reliability changes, operations probe and refreshed hosted walkthrough. 3D Humanities Lab independently reviewed the contributions and supplied the tested dependency, assertion, documentation and notice corrections. Upgrade by rebuilding API and web images while preserving `.env`, database and media volumes; these changes introduce no schema migration.

## v0.2.0

- Added an optional visual authoring console for mapping one video range to a captured model point, previewing privately and publishing the annotation individually.
- Clarified the generated demo's three chapters and added an explicit guarded refresh with rollback for existing synthetic installations.
- Preserved a manually chosen camera during direct pin playback and retained selection at a guided range's exclusive end.
- Corrected blank point validation, dark-mode branding, the retained muted-light palette and the generated Top edge placement.
- Refreshed clean transcript drafts from the latest server text, preserved unsaved edits during tab changes, and required explicit reconciliation before saving a conflicting transcript.
- Added the 3D Humanities Lab's complete-viewport authoring tutorial, poster, timed description and setup guide.
- Added Unlock Digital's hosted collection walkthrough, still poster, optional animated preview and feature guide, with explicit presentation permissions and reuse exclusions.
- Patched brace-expansion and PyJWT dependency pins with matching license notices, and expanded browser and real-API demo checks.
- Aligned the API, web package and software citation version metadata to 0.2.0, and clarified setup and security documentation for the source and earlier releases.

3D Humanities Lab contributed the generated-demo code, guarded refresh, dependency and transcript corrections, and local authoring tutorial. Unlock Digital contributed the hosted walkthrough recording/editing and assessed incorporation of the Lab-authored media notices. The hosted media retains separate presentation permissions; inclusion grants no reuse license for those assets or depicted research content.

## v0.1.1, September 29, 2026

- Corrected public static-asset permissions, added visible evidence loading progress and the muted-light theme, and improved public object ordering, mobile cards and pagination.
- Added the protected contribution and owner-controlled release process, including the four required CI checks.
- Replaced browser and citation fixtures with generated sample content and added a public-fixture boundary guard.

The [v0.1.1 release](https://github.com/3dhlab/loci/releases/tag/v0.1.1) combines Unlock Digital's contributions and its assessed adaptations of Lab proposals. All four checks passed on the final main commit and tag. Hosted operations, larger-collection performance and older source archives remain separate assessments.

## v0.1.0, September 27, 2026

- Added a local example linking points on a 3D model to video, transcripts, citations and share links.
- Included a generated cube, timed transcript and three annotations. One annotation plays two video sections in sequence.
- Added generated credentials, local application access and separate database and media storage.
- Made collection ordering and model-loading settings configurable.
- Added checks for installation, publication privacy, media replacement, database migrations and browser behavior.
- Documented setup, contribution, support, security and software citation.

Local and hosted validation passed. The first-party code and original sample terms are Apache 2.0. Further maintenance will focus on the larger frontend components and model-loading performance.
