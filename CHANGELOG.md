# Changelog

## Unreleased

- Repaired the public search response wrapper and made loading, error, empty, and results states exclusive. Added brief collection and 3D viewer guidance.
- Added configurable public sitemap generation for the homepage and object browser, with a matching sitemap entry in `robots.txt`.
- Added runtime filesystem capacity reporting and a privacy-limited public reader probe. See the [operations guide](docs/operations.md).
- Pinned the transitive `source-map-js` build dependency to patched version 1.2.2.

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
