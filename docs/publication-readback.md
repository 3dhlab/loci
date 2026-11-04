# Publication readback

The [durable publication guide](durable-publication.md) describes the current
request, worker, generation, reconciliation and recovery flow. Acceptance is
queued; only a matching current generation commitment with verified HTTP
readback produces a successful publication receipt. Earlier process-local
status and retry guidance does not describe this API.

## What is verified

The destination reads imported records in a fresh read-only, repeatable-read
transaction and compares selected fields, annotation playlists, stable
identifiers and citation text. It hashes the selected regular media files using
no-symlink checks. SHA256 and byte length identify content; modification times do
not. A fresh challenge binds each response to its request. Embedding vectors
and rederived corpus-phrase timestamps are outside this receipt.

The low-level `verify_public_projection` command still advertises
`loci-publication-readback`, version 1. It verifies a supplied projection; it does
not admit a publication, claim a worker operation or establish a durable
commitment. The current generation protocol, expected-head checks and
transactional receipts are described in the durable guide. Source, worker and
destination must run matching code; an incompatible protocol has no success
fallback.

Requests arrive through stdin rather than a mutable shared manifest. The
verification request is bounded to 32 MiB and response to 64 KiB; responses contain
digests and counts rather than private record text. These are protocol bounds,
not remote memory or CPU quotas. An unexpectedly large stored field can still
cost substantial memory, and timing out a local SSH child does not prove that
its remote execution stopped. Preserve uncertain outcomes for reconciliation.
A configured destination's receipt is not an independently signed attestation.

## Validation boundary

Component tests use generated records and injected database/SSH boundaries.
The disposable PostgreSQL/API suite separately exercises durable admission,
authorization, fencing, history retention and schema rollback refusal. Neither
establishes real two-sided SSH, production-worker lifecycle, public media and
citation delivery or integrated browser recovery. Those combined acceptance
checks remain required before supported institutional private-to-public deployment. See [contribution checks](../.github/CONTRIBUTING.md)
and the [durable guide's compatibility and testing limits](durable-publication.md).
