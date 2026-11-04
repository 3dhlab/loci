# Loci roadmap

The [v0.3.0 quickstart](https://github.com/3dhlab/loci/blob/v0.3.0/README.md#quickstart) installs a local demo with generated content, a public reader and optional visual authoring. The next release goal is a supported path for a small institution to author in private and publish approved material to its own public server. These milestones describe planned work.

1. **Private authoring to a public server.** Define the supported workflow from private preview and approval to public readback. Specify which records, media, transcripts, attribution and citations may cross the boundary, and how editors verify what readers can see.
2. **Deployment setup.** Prepare and validate a Docker and Compose setup for the authoring system and public destination. Document worker setup, restricted SSH access, domain and DNS configuration, HTTPS, secrets, persistent volumes and backup prerequisites.
3. **First publication and two generations.** Publish a first generation in a disposable source and target installation, then publish a revision through the same worker and SSH path. Verify public HTTP readback for both generations, including media, citations, receipts and the visibility of approved content.
4. **Failure and retry recovery.** Exercise interrupted transfers, lost acknowledgements and worker restarts. Verify that a retry or reconciliation respects current authority, retains operation history and avoids duplicate publication.
5. **Rollback with retained history.** Rehearse application rollback with published generations and receipts still present. Verify reader continuity and document which schema, media and history the operator must preserve.
6. **Scale, accessibility and operator documentation.** Measure publication and reader behavior with representative collection sizes. Check keyboard, screen-reader and mobile workflows. Use those results to finish installation, monitoring, recovery and rollback instructions an operator can follow.

## Later connectors

Evaluate **CollectionSpace**, **Drupal** and **Omeka** connectors after the core workflow has passed acceptance. Start with mappings for stable identifiers, metadata, rights, media and citations. Verify that connector imports and exports respect the same private-to-public boundary.
