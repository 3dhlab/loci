# Durable publication requests

Publication selects one generation of approved published records and media,
records that request in PostgreSQL, and then delivers its identity to the
publication worker. Acceptance means **queued**, not published. Redis can be
unavailable after acceptance: the durable row remains an outbox entry, and the
worker's recurring dispatcher resumes delivery after restart.

This guide describes implemented protocols and operating requirements. Turnkey
institutional private-to-public deployment is a next-release goal; the combined
acceptance checks below remain required.

Both API and worker need the same application version and migration
`20261103_0037`. The destination needs the corresponding generation/import and
readback protocol before promotion. The configured public base URL is required
for HTTP verification. Existing SSH host-key verification and active-user
publication permission remain in force; publication is not made admin-only.

## Request and recovery API

- `GET /api/v1/ops/live-database/sync/capabilities` is authenticated. Clients may
  persist a pending key only when both `durable_requests` and `request_lookup`
  are true. The server checks transport code and migrated ledger availability.
- `POST /api/v1/ops/live-database/sync/start` requires an `Idempotency-Key`
  header, with 1–256 ASCII letters, digits, dots, underscores, colons or hyphens
  and an alphanumeric first character. Omit the body; selectors are not
  supported. It returns `202` and the durable operation's status.
- The older `POST /api/v1/ops/live-database/sync` URL delegates to exactly the
  same admission path and also returns `202`. This changes its former
  synchronous completion semantics. It cannot bypass the ledger or worker.
- `GET /api/v1/ops/live-database/sync/status` with the original
  `Idempotency-Key` header recovers an accepted request when its acknowledgement
  was lost before the client learned its operation ID. The raw key is never
  placed in a URL or stored in the ledger.
- `GET /api/v1/ops/live-database/sync/operations/{operation_id}`, or status with
  the `operation_id` query parameter, reads that exact operation. With neither
  identity, status returns the current account's latest operation, or `IDLE`.
  Another account's operation/key and an unknown operation both return `404`.
- `POST /api/v1/ops/live-database/sync/{operation_id}/reconcile` checks an
  existing destination commitment and its current readback. It does not repeat
  a promotion. Only the owning active account can request this API action.
- `POST /api/v1/ops/live-database/sync/{operation_id}/cancel` cancels an unclaimed
  queued request locally. An uncertain admitted operation requires a permanent
  destination cancellation fence. It refuses a running operation and cannot
  undo a committed generation. Cancellation is an explicit action.
- `GET /api/v1/ops/live-database/sync/blocked` requires an active platform admin.
  It returns the blocking status without selection content or the original
  actor's personal details. An active admin may cancel another actor's queued
  or uncertain request, including after the original actor was revoked. This
  recovery request is audited before a target fence is attempted. Ordinary
  cross-account access remains unavailable.

A duplicate key for the same account/destination returns the original operation
and frozen selection. It never reselects current source records or media.
Different requests cannot occupy the global publication lane while one is
queued, running or uncertain. The database advisory lock and unique active-lane
index enforce this across API instances and browser tabs; browser storage is
not a lock or an authorization source.

The selection captures the destination's generation and monotonic head version.
The destination checks that expectation under its transaction lock before
applying anything. A changed destination produces a permanent `not_applied`
fence instead of overwriting a newer generation.

## Worker and destination boundaries

A worker conditionally claims a queued row under a database row lock, records
its unique attempt token and maintains a 300-second lease. A heartbeat renews
the lease every 30 seconds. Immediately before its single public apply, the transport calls
back into a fresh transaction that checks the current token, lease and actor's
active state, and commits effect admission before the remote command.
Effect admission is single-use, including within the same worker attempt.

The dispatcher runs in its own spawned process rather than a database thread
inside RQ's forking parent. Workhorses dispose inherited database pools before
using their own connections. Shutdown targets only the returned owned dispatcher
process, first cooperatively and then with one bounded terminate fallback.

After a worker dies before effect admission, recovery requeues the **same frozen
selection** and invalidates its old attempt token. After effect admission, an
expired or failed attempt becomes `NEEDS_RECONCILIATION`; recovery can only read
the destination. It cannot retry an unknown public effect. The uncertainty holds
the lane until a matching, current destination commitment and complete readback
are confirmed. An already committed effect can be confirmed by system recovery
after its original actor is revoked; that grants no new effect authority.

Readback and cancellation use a separate reconciliation token and renewed lease,
preserving the original effect attempt. Concurrent requests return the current
state without starting another target readback. Expired tokens cannot finalize
or release a newer reconciliation claim.

A confirmed `not_applied` fence resolves the source as `CANCELLED` and frees the
lane. Late delivery for that operation can only observe its fence. Orphan files
are retained; they are not adopted or deleted to force a retry. A matching
historical commitment and a newer destination head resolve an applied operation
as `SUPERSEDED`. This records prior database/files commitment and a different
current generation; it claims neither current HTTP verification nor an undo.
Neither resolution is exposed as a verified success receipt.

The target's configured byte allowance applies to a genuinely new apply, after
checking existing receipts and the expected destination head. It never blocks
identity-bound readback or cancellation. An over-limit rejected request can be
fenced, and lowering the allowance cannot make retained history unrecoverable.
The shared request parser still validates identity, metadata, digests, challenge
and the bounded request size in every mode.

The destination records the generation commitment in the same transaction as
its imported records. Its global head identifies the selected generation and
advances monotonically. Source completion requires matching operation,
generation and selection identities, record/media hashes and counts, plus
verified HTTP readback. A database commit alone is insufficient. Status exposes
the receipt only for `SUCCEEDED`; private source selection payloads and spool
paths are absent from the public status DTO.

Published video rows have an explicit generation identity with a deferred
foreign key, allowing their immutable rendition and receipt to commit together.
Ordinary authoring renditions retain their existing identity checks.

Same-project cross-object video annotations remain in the verified preview and
record projection. Stable evidence focus is checked only for annotations whose
video belongs to that evidence object, matching the existing public route.
Eligible same-object citations remain mandatory; unsupported cross-object
evidence URLs are not invented and their annotations are not dropped.

Transcript re-ingest may replace or reorder Segment/TranscriptWindow identities
under the same Transcript. Import frees obsolete child keys and temporarily
parks retained ordinals before assigning final values, all within the caller's
single transaction. Retained child reparenting is rejected before mutation.
Replacing a parent Transcript UUID at the same natural key is outside this
narrow compatibility repair and still requires a separately tested policy.

Readback dispatch excludes a live reconciliation lease and starts its existing
retry cooldown after an unconfirmed probe releases its claim. This prevents
known redundant dispatch during a long probe; it is not a general RQ fairness
guarantee. New and retry-opened writer directories sync their inode and parent
link; the final sealed mode is set before its sync. Syscall-order regression
tests do not establish power-loss behavior on every storage backend.

Video projections freeze only the existing public citation-attribution bands:
public speaker label, session date/text, precision/mode and time bounds. Raw OCR,
presenter records and private provenance are excluded. A generation with missing
or invalid attribution fails closed; an explicit empty snapshot suppresses stale
destination title cards. New legacy projections also honor a present validated
snapshot. Truly old payloads without the field retain their former live-policy
behavior, as do genuine authoring rows without a projection snapshot.

The new job's immutable selection and destination receipt are its publication
history. This path preserves existing `PublicationManifest` history rows but
does not update their legacy current/projected pointers. Those rows must not be
presented as the current durable generation. A compatibility adapter must derive
any later legacy-history update from the frozen selection, without reselecting
live records; consumers relying on those pointers need explicit acceptance.

## Upgrade, rollback and acceptance

The source API and worker require `SEMANTIC_PUBLIC_SYNC_PUBLIC_BASE_URL`, the
trusted HTTP(S) origin used for anonymous public readback, without a path, query,
fragment or credentials. The transport reads it from the existing private sync
environment file selected by `SEMANTIC_PUBLIC_SYNC_ENV_FILE`, or from the source
process environment. Both source processes must receive the same sync settings
and share their persistent media volume so a restarted worker can read the
request's sealed selection.

The shipped Compose files do not start a publication worker automatically. Once
the matching API image and migration are installed and its PostgreSQL/Redis
services are running, an operator can start the source worker in the foreground
using the API service's existing environment, network and media mounts:

```sh
docker compose run --rm --no-deps api python -m app.worker.main
```

This creates a separate worker container without publishing the API service's
ports. Keep that process running while publication is enabled; it starts both
RQ consumption and the recurring durable-outbox dispatcher. Queued acceptance
alone does not establish that a worker is available. Interrupt it gracefully
and allow owned worker/dispatcher cleanup to finish before replacing its image.
A dedicated public-reader destination does not need this source worker. This
command documents an operator-controlled startup procedure; no hosted service
is started by an upgrade or by these instructions. The actual production CLI,
forked workhorse and two-sided transport require disposable acceptance tests.

Set `PUBLICATION_DESTINATION_ENABLED=true` only in the dedicated public-projection
target API process. It defaults to false; protocol, head, apply and recovery
commands refuse an unconfigured target. Do not enable it on an authoring database.
`PUBLICATION_SELECTION_MAX_BYTES` limits selected media bytes per request and
defaults to 50 GiB. Configure an appropriate allowance independently on source
and target; the target can reject a selection that exceeds its own allowance.
This per-request limit does not allocate storage or limit the aggregate retained
selection and generation history.

The transport uses the installation's external `docker-compose.public.yml` and
`.env.public`; these deployment files are not supplied by this repository.
An environment file alone does not prove that Compose forwards a setting into
the API container. Verify effective target settings, the compatible migrated API
image, and existing volume mappings in the disposable acceptance environment.
The host import root must map to `/var/lib/semantic/import`, and the target API
must resolve its persistent media root consistently with imported generation
paths. Keep source selections and target generation seals private: expose only
the authorized public API media routes, not a generic directory listing or
static mount of `.loci-publication-selections` or generation metadata. These are
configuration requirements for later acceptance and deployment review, not
evidence that a running installation has been updated or tested.

The migration adds the private request ledger, immutable destination commitment
history and the destination head. It does not rewrite existing collection rows.
The schema refuses deletion or rebinding of retained history. Downgrade is
allowed only while both history tables and the destination head are empty.
After publication activity,
code rollback must retain the additive schema and receipts; do not discard them
to force a downgrade. Empty-database upgrade/downgrade/re-upgrade and
populated-history downgrade refusal are tested. Application/code rollback with
retained history remains unvalidated, including older API and worker versions
reading new jobs, generations and media. Compatibility with external consumers
of legacy `PublicationManifest` current/projected pointers is also unproved.
Keep source, worker and target versions compatible and resolve uncertain
operations before permitting other publication tools to write the same destination.

Generated contract and worker-boundary tests exercise validation and orchestration.
The CI job also runs pure guards for the synthetic fixture and production-worker
process helpers in `scripts/ci/publication_acceptance_fixture.py` and
`scripts/ci/publication_worker_cli.py`. Seeding requires explicit GitHub-hosted
Linux eligibility before application imports or writes. Worker startup requires
fresh, independently inspected owned PostgreSQL/Redis resources and an empty
Redis receipt. The helpers do not provision those services or establish a full
acceptance result. The existing workflow's helper-guard step starts no worker,
encoder or SSH server. A real SSH/destination/HTTP controller and reviewed
disposable setup remain required; neither the helpers nor mocked callbacks can
stand in for that missing acceptance execution.

The PostgreSQL/API module additionally covers admission replay/conflict,
concurrent sessions/claims, actor isolation, revocation, effect fencing,
lease recovery, a separate-process crash, receipt rejection, immutable history
and migration rollback/re-upgrade, cancellation fences, superseded history and
admin recovery privacy. Its transport is generated: these cases do
not establish actual SSH/HTTP/media delivery or RQ process recovery. Supported institutional
private-to-public deployment requires the combined candidate's two-sided transport,
worker/Redis restart, private preview, browser reload/retry, media and citation
readback, privacy, migration/rollback and all required CI checks.
