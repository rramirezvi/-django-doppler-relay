# Bulk V2 Send State Machine Specification

## Purpose

Defines the durable per-recipient send lifecycle for V2 real-send, and the
invariants that make impossible states impossible. `BulkSendRecipient`
(`relay/models.py:301-382`) already carries an import-only `status` field
(`pending | invalid`), protected by the DB `CheckConstraint`
`bulk_recipient_valid_status` (`relay/models.py:339-342`). This capability
MUST NOT reuse `status` for send state and MUST NOT alter
`bulk_recipient_valid_status`. It introduces an independent field,
`send_status`, with its own lifecycle, used exclusively for send
progress. Whether each invariant below is enforced by a DB constraint or
a service-layer check is a decision left to `sdd-design`; this capability
requires only that every invariant be enforced and tested by some
mechanism, so that an impossible state can never be persisted and
observed.

Assume zero server-side deduplication from Doppler
(`DOPPLER_SERVER_SIDE_IDEMPOTENCY = NOT_DOCUMENTED` — confirmed against
api.dopplerrelay.com docs and against `send_template_message`,
`relay/services/doppler_relay.py:546-732`, which sends no client-reference
or idempotency field today). Every duplicate-send guarantee specified
below is therefore local and durable, never assumed on Doppler's side.

Out of scope for this capability: any modification to V1's `EmailMessage`,
`Delivery`, or `process_bulk_id` (`relay/services/bulk_processing.py:39-42`
stays untouched); any modification to `BulkSendRecipient.status` or
`bulk_recipient_valid_status`; V2 scheduled send; V2 batch real-send above
one recipient; and automatic retry of an `ambiguous` outcome. The
one-recipient execution ceiling for the canary itself is specified in
`bulk-v2-real-send-authorization`; the safety properties in this
capability MUST hold generally (they are not conditioned on
`MAX_ROWS == 1`), because they are the structural difference this
initiative exists to prove between V1's CSV-driven batch retry and V2's
DB-driven recipient state machine.

## Requirements

### Requirement: Dedicated send_status Field Independent From Import status

`BulkSendRecipient` MUST gain an independent field, `send_status`, used
exclusively for V2 send progress. `status` (`pending | invalid`) and its
existing `bulk_recipient_valid_status` `CheckConstraint` MUST remain
unchanged in meaning, values, and enforcement.

#### Scenario: Import outcome and send outcome are recorded independently

- GIVEN a `BulkSendRecipient` row with `status == "pending"` (a valid
  import outcome)
- WHEN its `send_status` transitions through the send lifecycle
- THEN `status` MUST remain `"pending"` throughout, unaffected by any
  `send_status` transition

#### Scenario: The existing import status constraint is untouched

- GIVEN the migration that introduces `send_status` is inspected
- WHEN its schema operations are enumerated
- THEN `bulk_recipient_valid_status` MUST NOT be dropped, redefined, or
  widened, and `status`'s allowed values MUST remain exactly
  `pending`/`invalid`

### Requirement: status=invalid Can Never Enter The Send State Machine

A `BulkSendRecipient` row with `status == "invalid"` MUST NEVER advance
`send_status` to `sending` or `sent`. This MUST be an explicit, testable
invariant, enforced by some mechanism (DB constraint or service-layer
check — `sdd-design`'s choice) and covered by a test that attempts the
forbidden transition directly.

#### Scenario: Claiming logic excludes invalid rows

- GIVEN a `BulkSendRecipient` row with `status == "invalid"` and
  `send_status == "not_started"`
- WHEN the worker's row-claiming logic selects the next eligible
  recipient for a `BulkSend`
- THEN that row MUST NOT be selected, and `send_status` MUST remain
  `"not_started"`

#### Scenario: A direct attempt to advance an invalid row is rejected

- GIVEN a `BulkSendRecipient` row with `status == "invalid"`
- WHEN any code path attempts to set its `send_status` to `"sending"` or
  `"sent"`
- THEN the attempt MUST be rejected before the row is persisted in that
  state, regardless of which layer enforces the rejection

### Requirement: Valid Forward-Only Transition Graph

`send_status` MUST follow exactly this graph: `not_started -> sending ->
sent | send_failed | ambiguous`. No transition MUST exist that moves a
row backward from a terminal or in-flight state toward an earlier state
in the graph — in particular, `sent` MUST be immutable going forward: no
code path MUST transition a `sent` row back to `sending` or to any other
state.

#### Scenario: Only the defined forward edges are reachable

- GIVEN a `BulkSendRecipient` row at any point in its lifecycle
- WHEN its `send_status` transitions are enumerated across the full test
  suite for this capability
- THEN every observed transition MUST be one of: `not_started ->
  sending`, `sending -> sent`, `sending -> send_failed`, `sending ->
  ambiguous`

#### Scenario: A sent row rejects any further transition

- GIVEN a `BulkSendRecipient` row with `send_status == "sent"`
- WHEN any code path attempts to change its `send_status` to any other
  value, including back to `"sending"`
- THEN the attempt MUST be rejected and the row MUST remain `"sent"`

### Requirement: Required Companion Data Per State

Each `send_status` value MUST carry the companion data that makes it
verifiable, not merely asserted:

- `sending` MUST have a `send_started_at` timestamp set.
- `sent` MUST have a `sent_at` timestamp set.
- `send_failed` MUST carry a non-empty error code.
- `ambiguous` MUST carry evidence of a prior attempt, and MUST be
  reachable only from `sending` — never directly from `not_started`.

#### Scenario: sending without send_started_at is not a valid state

- GIVEN a row is transitioned to `send_status == "sending"`
- WHEN the transition is persisted
- THEN `send_started_at` MUST be non-null in the same persisted state; a
  `sending` row with a null `send_started_at` MUST NOT be reachable

#### Scenario: sent without sent_at is not a valid state

- GIVEN a row is transitioned to `send_status == "sent"`
- WHEN the transition is persisted
- THEN `sent_at` MUST be non-null in the same persisted state; a `sent`
  row with a null `sent_at` MUST NOT be reachable

#### Scenario: send_failed without an error code is not a valid state

- GIVEN a row is transitioned to `send_status == "send_failed"`
- WHEN the transition is persisted
- THEN a non-empty error code MUST be present in the same persisted
  state; a `send_failed` row with no error code MUST NOT be reachable

#### Scenario: ambiguous is unreachable without prior sending evidence

- GIVEN a row currently at `send_status == "not_started"`
- WHEN any code path attempts to transition it directly to
  `send_status == "ambiguous"`, skipping `sending`
- THEN the attempt MUST be rejected; `ambiguous` MUST only be reachable
  as a terminal outcome of a row that was previously `sending`

### Requirement: Send Attempt Persisted Durably Before Any Outbound Call

A send attempt (the row's transition to `sending`, its attempt identity,
and its `send_started_at`) MUST be committed to the database before the
outbound Doppler call is made. "Durably recorded" means: if the process
crashes immediately after that commit returns, a subsequent process reading
the same database MUST observe the row as `sending` with its attempt data
intact — the record's existence MUST NOT depend on anything held only in
process memory.

#### Scenario: Worker crash before the outbound call leaves a recoverable record

- GIVEN a worker has committed a row's transition to `sending` with
  `send_started_at` set
- WHEN the worker process crashes before making the Doppler call
- THEN a subsequent read of that row from the database MUST show
  `send_status == "sending"` with `send_started_at` populated, with no
  outbound call ever having been made

#### Scenario: No outbound call is ever made against an uncommitted attempt

- GIVEN a worker is about to select a row to send
- WHEN it evaluates whether to make the Doppler call
- THEN the `sending` transition and its attempt data MUST already be
  committed in the database before that call begins; the call MUST NOT
  be made first and persisted after

### Requirement: Single-Writer Claiming Prevents Concurrent Processing Of One Row

Claiming an eligible row MUST be a conditional, row-locked transition
(`not_started -> sending`) such that two concurrent workers can never both
win the same row.

#### Scenario: Two workers race for the same eligible row

- GIVEN two workers start concurrently and both observe the same row at
  `send_status == "not_started"`
- WHEN both attempt to claim that row at the same time
- THEN exactly one worker MUST succeed in transitioning it to `sending`
  and making the Doppler call; the other MUST observe the claim as
  already taken and MUST NOT also transition the row or call Doppler

### Requirement: Terminal And In-Flight Rows Are Immune To Job Retry And Double Execution

A `BackgroundJob` retry or a duplicate execution of the same job MUST
never cause a second Doppler call for any recipient row already past
`not_started`. Retries and duplicate executions MUST only ever act on
rows still at `not_started`.

#### Scenario: BackgroundJob retry only touches non-terminal rows

- GIVEN a `BulkSend`'s `BackgroundJob` is retried after one recipient row
  already reached `send_status == "sent"` and another is still
  `not_started`
- WHEN the retried job runs
- THEN it MUST NOT re-attempt the `sent` row (no second Doppler call for
  it), and MUST only process the row still at `not_started`

#### Scenario: The same BackgroundJob executed twice does not double-send

- GIVEN the same `BackgroundJob` id is dispatched twice, whether through
  `run_background_job` bypassing `claim_next_job`'s row lock
  (`relay/services/jobs.py:68-69,72-87`) or through any other duplicate
  invocation
- WHEN both executions attempt to process the same recipient row
- THEN at most one Doppler call MUST occur for that row across both
  executions, because the second execution MUST observe the row already
  past `not_started` and skip it

### Requirement: Stale Sending Rows Never Auto-Resolve; They Become Operator-Visible

A row that has remained in `sending` beyond any reasonable duration MUST
NEVER automatically transition back to `not_started` or to any other
state. Instead, it MUST become observable to an operator as stale —
through the recipient ledger and/or the logging events specified in
`bulk-v2-real-send-observability` — so a human, not a machine, decides its
resolution.

#### Scenario: Service restart with a row left in sending

- GIVEN `django.service` restarts while a recipient row is at
  `send_status == "sending"`
- WHEN the system comes back up
- THEN that row MUST remain exactly `send_status == "sending"` with its
  original `send_started_at`; no startup or scheduled process MUST reset
  it to `not_started` or advance it to `sent`

#### Scenario: An aged sending row is discoverable without guessing

- GIVEN a row has been at `send_status == "sending"` for longer than any
  plausible single HTTP attempt to Doppler
- WHEN an operator queries `BulkSendRecipient` rows for the targeted
  `BulkSend`
- THEN the row's `send_status` and `send_started_at` alone MUST be
  sufficient to identify it as stale, requiring no CSV, log tailing, or
  process-memory state to detect

### Requirement: Crash Recovery Reconstructs Status From Persisted State Alone, Zero CSV Dependency

After a worker crash and restart, the system MUST be able to determine
the send status of every recipient using only persisted database state.
No recovery path MUST depend on the original CSV, on `BackgroundJob`
being the source of truth for progress, or on anything held in process
memory.

#### Scenario: Doppler accepts but the worker dies before persisting sent

- GIVEN a worker made the Doppler call, Doppler's response was accepted
  server-side, but the worker process died before it could persist
  `send_status == "sent"`
- WHEN a new worker or operator inspects that row after restart
- THEN the row MUST still read as `sending` (or, once evaluated per the
  ambiguity requirement below, `ambiguous`) — it MUST NOT be silently
  reported as `sent` on the basis of an assumption, only on the basis of
  correlatable evidence

#### Scenario: Worker crash after persisting sent survives restart correctly

- GIVEN a worker successfully persisted `send_status == "sent"` with
  `sent_at` set, and then the process crashed
- WHEN the system restarts and a new worker or the management command
  re-evaluates the same `BulkSend`
- THEN the row MUST be read as `sent` from the database alone, and MUST
  NOT be re-attempted, re-queued, or re-sent

#### Scenario: Recovery never opens the CSV

- GIVEN a full worker crash and restart with recipients in every possible
  `send_status`
- WHEN the recovery path determines what was sent, not sent, failed, and
  ambiguous
- THEN it MUST do so by reading `BulkSendRecipient` rows only; the
  original `recipients_file` MUST NOT be opened, parsed, or referenced by
  any part of recovery

### Requirement: Ambiguous Outcomes Require Human Resolution With Correlatable Evidence

A `send_status` of `ambiguous` MUST result whenever local certainty about
Doppler's acceptance cannot be established (timeout, connection loss,
unparseable or missing response — anything where acceptance cannot be
ruled out). An `ambiguous` row MUST NEVER be automatically retried or
automatically resolved. Resolving it MUST require a human, using evidence
that correlates the local attempt to a queryable Doppler record. If that
correlation cannot be demonstrated (an open evidence question for
`sdd-design`), the row MUST stay `ambiguous` and the canary MUST abort
without re-sending — no fallback, no "probably fine", no timed
auto-resolution.

#### Scenario: Worker dies during the outbound call

- GIVEN a worker committed `send_status == "sending"` and then the
  process died mid-call, so it is unknown whether Doppler received or
  processed the request
- WHEN the row is next evaluated (by a new worker, the management
  command, or an operator)
- THEN it MUST NOT be assumed `sent` or `send_failed`; it MUST be
  evaluated toward `ambiguous` once correlatable evidence is sought and
  found insufficient

#### Scenario: Management command encounters an ambiguous row and does not touch it

- GIVEN a targeted `BulkSend` has a recipient row at
  `send_status == "ambiguous"`
- WHEN the management command runs against that `BulkSend`
- THEN it MUST report the row as `ambiguous` and MUST NOT resend, retry,
  or otherwise act on it — consistent with the command never offering
  `--retry-ambiguous` or `--force`

#### Scenario: Correlation cannot be demonstrated

- GIVEN an `ambiguous` row for which no identifier or query against
  Doppler can be shown to unambiguously confirm or deny delivery
- WHEN a human attempts to resolve it
- THEN the row MUST remain `ambiguous`, the canary execution MUST be
  treated as aborted, and no automatic or manual re-send MUST occur as a
  substitute for that missing evidence

### Requirement: Attempt Modeling Is A Design Decision, Bound By Functional Requirements

Whether the send attempt is tracked via fields directly on
`BulkSendRecipient` or via a separate `SendAttempt` model is left to
`sdd-design`. Whichever modeling choice is made MUST satisfy, at minimum:
durability of the attempt record before the outbound call (per the
durable-attempt requirement above), a monotonically increasing attempt
number per recipient, and enough auditability to reconstruct, after the
fact, exactly when each attempt started, what its outcome was, and — for
`sent` — what identifier Doppler returned.

#### Scenario: Attempt history is reconstructable regardless of modeling choice

- GIVEN the chosen modeling approach (fields on `BulkSendRecipient`, or a
  separate attempt model) has been implemented
- WHEN an auditor reconstructs the full attempt history for one recipient
  row after the canary execution
- THEN the reconstruction MUST show attempt number, start time, and
  terminal outcome for every attempt made against that row, without
  requiring any source outside the database

### Requirement: Correlatable Doppler Identifier Must Be Persisted

The system MUST persist whatever identifier Doppler returns on accept,
and that identifier MUST be usable to query delivery status afterward.
Verifying which exact identifier Doppler returns, and confirming it is
genuinely queryable post-hoc, is a design-phase evidence requirement (per
the proposal's non-negotiable floor): if that evidence cannot be produced
in `sdd-design`, the send-state machine MUST default to treating
uncorrelated outcomes as `ambiguous` rather than `sent`.

#### Scenario: A sent row carries the returned identifier

- GIVEN a row reaches `send_status == "sent"`
- WHEN the persisted row is inspected
- THEN it MUST contain the identifier Doppler returned for that call,
  stored in a field usable for a later correlation query

#### Scenario: Missing correlation evidence blocks a sent classification

- GIVEN `sdd-design` has not produced evidence that a given identifier is
  genuinely queryable after the fact
- WHEN the send-state machine is implemented
- THEN it MUST NOT classify an uncertain outcome as `sent` on the basis
  of that unproven identifier; the outcome MUST default to `ambiguous`

### Requirement: Migration Is Additive And Reversible Without Affecting V1 Or V2 Import

The migration that introduces `send_status` and its companion fields
MUST be additive (new columns and, if used, a new job-type choice on
`BackgroundJob`) and reversible. Reverting it MUST NOT alter or remove
any V1 behavior, any existing V2 import behavior, or the existing
`bulk_recipient_valid_status` constraint.

#### Scenario: Reverse migration leaves V1 and V2 import untouched

- GIVEN the send-state migration has been applied
- WHEN it is reverse-migrated
- THEN existing V1 send behavior and existing V2 import behavior MUST be
  byte-identical to their pre-migration behavior, and
  `bulk_recipient_valid_status` MUST remain exactly as it was before this
  change
