# Publication destination readback

The SSH publication transport verifies the destination's committed record fields
and selected media bytes before marking local package history projected. It also
checks the destination on a no-change sync: the saved sync-state JSON and an SSH
exit code are insufficient evidence that the public copy still matches.

The source first validates the complete readback request and checks destination
protocol `loci-publication-readback`, version 1. The destination must receive the
matching application code before the source uses this protocol. A missing or
incompatible protocol fails before remote directory creation, upload, media
installation, import or pruning. There is no fallback that reports success.
This requires a coordinated source/destination upgrade; no deployment or hosting
configuration change is performed by the code change itself.

After promotion, the destination opens a fresh read-only, repeatable-read database
transaction and reads the imported row sets independently of the import
transaction. It compares every selected imported field, including annotation
playlists, stable object/annotation/clip identifiers and stored citation text.
Search embedding vectors and rederived corpus-phrase timestamps are explicitly
outside this receipt. Numeric values follow the PostgreSQL column scale and
rounding. File-derived clip posters are verified through the media inventory.

The destination hashes all selected regular media files under the configured
media root using the no-symlink inventory checks. Receipt identities use SHA256
and byte length; filesystem modification times do not identify content. A fresh
challenge binds the response to the current readback. The expected selection is
sent through stdin rather than reloading a shared mutable remote manifest file.
The request is limited to 32 MiB and response to 64 KiB, with a bounded caller
wait. The reader selects only compared columns and at most the expected row count
plus one per table; it does not materialize excluded embedding vectors. These
are not remote memory/CPU quotas: an unexpectedly large stored field can still
require substantial memory. Killing or timing out the local SSH child does not
establish that a remote Docker execution has stopped. Remote workload termination,
query timeouts and large-collection resource behavior require integration tests.
Successful receipts contain digests and counts, not record text or credentials;
they are retained with the local completion/no-change audit event and sync state.

Publication retains the existing active-user permission policy. It rereads that
user's current active state before transport and before each public media/import/
prune effect. This observation is not a fenced authority lease: revocation can
race a command already in flight. Trusted internal system calls remain supported.

A failure after a public effect was attempted, a mismatched/lost readback, or a
local completion-commit failure leaves an unverified outcome. Both synchronous
and asynchronous paths mark the current process `NEEDS_RECONCILIATION`; its
start actions refuse another promotion. The interface must not call that state
published or offer an ordinary retry. Do not clear uncertainty by restarting the
process or infer that a failed SSH command had no effect.

This remains a partial implementation of the full publication release. The
current status and gate are process-local. Durable admission, selected-generation
sealing, worker dispatch/claims, cross-process fencing, restart reconciliation and
an operator recovery path are still required. The database and media changes are
not an atomic generation switch. This receipt is an observation by the configured
SSH destination, not an independently signed artifact, an idempotent destination
commitment, or proof of public HTTP/model/video/citation delivery. Real two-sided
database/SSH/HTTP tests, privacy and authorization journeys, crash/restart,
migration/rollback and required CI must pass before a full release.

The focused unit tests use generated records and local benign subprocesses. Sync
orchestration tests execute the actual function bodies with substituted database
and SSH boundaries; they do not replace the required integration tests. No schema,
production configuration, credential or hosting change accompanies this slice.
