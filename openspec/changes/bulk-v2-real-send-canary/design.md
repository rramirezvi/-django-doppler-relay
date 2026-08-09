# Design: Bulk V2 Real-Send Canary

Derived from `openspec/changes/bulk-v2-real-send-canary/proposal.md` (Engram
`sdd/bulk-v2-real-send-canary/proposal`, obs #355) and the three delta specs
under `openspec/changes/bulk-v2-real-send-canary/specs/` (Engram
`sdd/bulk-v2-real-send-canary/spec`, obs #356), constrained by
`sdd/bulk-v2-real-send-canary/explore` (#353) and
`sdd/bulk-v2-real-send-canary/blockers-resolved` (#354).

Architecture only. No code written, no migration created or run, no flag
activated, no production access, no Doppler call, no email sent in this phase.

The spec fixed **what must hold**. This document fixes **how**, as closed
decisions. Where a decision cannot be closed with evidence available in this
phase, that is stated explicitly as an unverified assumption with its fallback
(§10), not left as an open question.

---

## 1. Context and the one structural idea

V1 is a *CSV-driven batch retry*: `background_job_retry` re-enters
`process_bulk_id`, which re-reads the whole file, and
`DopplerRelayClient._request()` (`relay/services/doppler_relay.py:224-293`)
retries up to three times on `RequestException`/`DopplerRelayError` — which can
re-POST after Doppler already accepted a message whose response was lost.

V2 real-send is a *DB-driven per-recipient state machine*. Everything below is a
consequence of three facts, all confirmed rather than assumed:

| Fact | Evidence |
|---|---|
| Doppler documents no server-side deduplication, and the client sends no idempotency/client-reference field | `send_template_message` payload build, `doppler_relay.py:606-617`; docs review recorded in #354 |
| `BackgroundJob` has no lease, heartbeat, or running-timeout, and `run_background_job(job_id)` bypasses `claim_next_job()`'s row lock | `relay/services/jobs.py:68-69` vs `72-87` |
| `BulkSendRecipient.status` is pinned to `pending`/`invalid` by a live DB `CheckConstraint` the V2 import path already depends on | `relay/models.py:321-325, 339-342` |

Therefore: duplicate-send safety must be **ours**, **durable**, and anchored on
the **recipient row**, not on the job and not on the file.

---

## 2. D1 — `send_status` schema

### 2.1 New fields on `BulkSendRecipient` (nine, all additive)

```python
class BulkSendRecipient(models.Model):
    # ... existing fields unchanged ...

    SEND_NOT_STARTED = "not_started"
    SEND_SENDING = "sending"
    SEND_SENT = "sent"
    SEND_FAILED = "send_failed"
    SEND_AMBIGUOUS = "ambiguous"
    SEND_STATUS_CHOICES = (
        (SEND_NOT_STARTED, "Not started"),
        (SEND_SENDING, "Sending"),
        (SEND_SENT, "Sent"),
        (SEND_FAILED, "Send failed"),
        (SEND_AMBIGUOUS, "Ambiguous"),
    )
    SEND_TERMINAL = frozenset({SEND_SENT, SEND_FAILED, SEND_AMBIGUOUS})
    SEND_STARTED = frozenset({SEND_SENDING, SEND_SENT, SEND_FAILED, SEND_AMBIGUOUS})

    send_status = models.CharField(
        max_length=16, choices=SEND_STATUS_CHOICES, default=SEND_NOT_STARTED
    )
    send_attempt_number = models.PositiveIntegerField(default=0)
    send_started_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    send_error_code = models.CharField(max_length=64, blank=True, default="")
    send_error_message = models.CharField(max_length=255, blank=True, default="")
    send_message_id = models.CharField(max_length=128, blank=True, default="")
    send_location = models.CharField(max_length=255, blank=True, default="")
    send_job_id = models.BigIntegerField(null=True, blank=True)
```

Per-field rationale for the non-obvious ones:

- **`send_attempt_number`** — required by the spec ("monotonically increasing
  attempt number per recipient") and load-bearing for the `ambiguous`
  prior-attempt invariant: `>= 1` *is* the durable evidence that an attempt
  existed. Incremented with `F("send_attempt_number") + 1` inside the claim.
- **`send_message_id` / `send_location`** — the two correlation artefacts the
  existing client can produce, kept separately rather than collapsed. See §10.
  `send_location` is the raw `Location` response header; it is a URL containing
  no credential and no recipient data, and it is self-describing for a later
  reconciliation query.
- **`send_job_id`** — a plain `BigIntegerField`, **deliberately not a
  `ForeignKey`**. Rationale: `BackgroundJob` is orchestration, not the source of
  truth (proposal principle 3). An FK would create a referential dependency in
  the wrong direction — deleting or cascading a job row must never touch or
  constrain the send ledger, and the ledger must stay readable when the job row
  is gone. A bare correlation integer records "which job made this attempt"
  without granting the job any authority over the record.
- **No response-body snapshot field.** `DopplerRelayError.payload`
  (`doppler_relay.py:185-194`) contains `request_headers` — including
  `Authorization: <scheme> <API_KEY>` — and `request_body`, the full rendered
  payload with recipient addresses and template variables. Persisting it would
  put a live credential in the database. Only `send_error_code` (classified) and
  `send_error_message = str(exc)[:255]` are stored; `exc.payload` is never
  persisted and never logged (§11).

### 2.2 New index

```python
models.Index(fields=["bulk_send", "send_status"], name="bulk_recipient_send_status_idx")
```

Serves both hot paths: the claim query (`bulk_send_id` + `send_status`) and the
operator ledger query. The existing `bulk_recipient_status_idx` (on
`bulk_send, status`) is untouched and does not serve these, because `status` and
`send_status` are different columns with different lifecycles.

### 2.3 The six invariants: DB constraint versus service layer

**Governing rule: an invariant that is a property of a single persisted row is a
DB `CheckConstraint`; an invariant that requires knowing the row's *previous*
value is a service-layer compare-and-set, because a row-level `CHECK` cannot
observe the prior value.**

| # | Invariant (from spec) | Mechanism | Why |
|---|---|---|---|
| 1 | `status == "invalid"` can never progress past `not_started` | **DB constraint `bulk_recipient_invalid_never_sends`** *and* service-layer filter in the claim | Row-shape property over two columns of the same row — natively expressible. Enforced twice on purpose: the claim filter prevents selection, the constraint makes the state *unpersistable* by any path, including `bulk_create`, raw ORM writes, admin, or a shell. See §2.4. |
| 2 | `sent` requires `sent_at` | **DB constraint `bulk_recipient_sent_requires_sent_at`** | Row-shape property. Also asserts the inverse (`sent_at` is NULL unless `sent`), so a stray timestamp cannot imply a send that never happened. |
| 3 | `sending` requires `send_started_at` | **DB constraint `bulk_recipient_send_started_at_consistent`** | Row-shape property, expressed as a biconditional over the whole graph, not just `sending`: `not_started ⇔ send_started_at IS NULL`. |
| 4 | `ambiguous` requires evidence of a prior attempt, reachable only from `sending` | **Split**: the *evidence* half is DB (`send_started_at` non-null via #3, `send_attempt_number >= 1` via `bulk_recipient_send_attempt_number_consistent`, non-empty error code via #5); the *reachable only from `sending`* half is service-layer compare-and-set | The evidence half is row-shape. "Only from `sending`" is a transition property — it needs the previous value, so no `CHECK` can express it. |
| 5 | `send_failed` requires a non-empty error code | **DB constraint `bulk_recipient_send_outcome_requires_error_code`** | Row-shape property. Strengthened beyond the spec to cover `ambiguous` as well, at zero cost: an `ambiguous` row without a reason class is useless to the human who must resolve it. |
| 6 | No `sent -> sending` transition (and no other backward edge) | **Service layer: compare-and-set `UPDATE ... WHERE send_status = <expected_from>`**, plus a `save()` guard mirroring the model's existing immutability convention | Transition property. See §2.5 for why a DB trigger was rejected. |

### 2.4 The six new `CheckConstraint`s, verbatim

Added to `BulkSendRecipient.Meta.constraints`, appended after the existing four.
Django 5.2 `condition=` keyword, matching the repo's current usage at
`relay/models.py:339-350`.

```python
models.CheckConstraint(
    condition=models.Q(send_status__in=[
        "not_started", "sending", "sent", "send_failed", "ambiguous"
    ]),
    name="bulk_recipient_valid_send_status",
),
models.CheckConstraint(
    # status=invalid can never progress past not_started.
    condition=models.Q(send_status="not_started") | ~models.Q(status="invalid"),
    name="bulk_recipient_invalid_never_sends",
),
models.CheckConstraint(
    # not_started <=> send_started_at IS NULL
    condition=(
        models.Q(send_status="not_started", send_started_at__isnull=True)
        | (~models.Q(send_status="not_started") & models.Q(send_started_at__isnull=False))
    ),
    name="bulk_recipient_send_started_at_consistent",
),
models.CheckConstraint(
    # not_started <=> attempt 0; every started state has >= 1 attempt.
    condition=(
        models.Q(send_status="not_started", send_attempt_number=0)
        | (~models.Q(send_status="not_started") & models.Q(send_attempt_number__gte=1))
    ),
    name="bulk_recipient_send_attempt_number_consistent",
),
models.CheckConstraint(
    # sent <=> sent_at IS NOT NULL
    condition=(
        models.Q(send_status="sent", sent_at__isnull=False)
        | (~models.Q(send_status="sent") & models.Q(sent_at__isnull=True))
    ),
    name="bulk_recipient_sent_requires_sent_at",
),
models.CheckConstraint(
    condition=(
        ~models.Q(send_status__in=["send_failed", "ambiguous"])
        | ~models.Q(send_error_code="")
    ),
    name="bulk_recipient_send_outcome_requires_error_code",
),
```

All six columns referenced (`send_status`, `status`, `send_started_at`,
`send_attempt_number`, `sent_at`, `send_error_code`) are `NOT NULL` or explicitly
nullable with the nullability itself under constraint, so there is no three-valued
logic hole where a `NULL` makes a `CHECK` evaluate to `UNKNOWN` and pass.

Every row shape produced by the existing V2 import path satisfies all six, which
is what makes the migration safe on a populated table:
`send_status="not_started"`, `send_started_at=NULL`, `send_attempt_number=0`,
`sent_at=NULL`, `send_error_code=""` — including for `status="invalid"` rows,
which satisfy `bulk_recipient_invalid_never_sends` via its first disjunct.

### 2.5 Transition enforcement, and why not a DB trigger

Every `send_status` write in the system goes through exactly one module,
`relay/services/bulk_v2_send_state.py`, and every write is a **conditional
`UPDATE` that names the expected current state in its `WHERE` clause**:

```python
updated = (
    BulkSendRecipient.objects
    .filter(pk=row_pk, send_status=BulkSendRecipient.SEND_SENDING)   # expected FROM
    .update(send_status=BulkSendRecipient.SEND_SENT, sent_at=now,
            send_message_id=message_id, send_location=location,
            updated_at=now)
)
if updated != 1:
    raise SendStateTransitionError("sending -> sent rejected: row not in sending")
```

This is a database-enforced transition guard even though it is not a declared
constraint: the `WHERE` predicate is evaluated by the database inside the same
statement that performs the write, so there is no read-then-write window. A
second actor holding a stale in-memory `send_status` cannot win, because its
`UPDATE` matches zero rows.

Note `.update()` bypasses `Model.save()` and therefore does not fire
`auto_now`; `updated_at=now` is passed explicitly in every transition. This is a
correctness detail, not a style choice — omitting it would silently freeze
`updated_at` on the only rows anyone will ever inspect.

Two supporting mechanisms:

1. **`save()` guard**, mirroring the model's existing immutability convention at
   `relay/models.py:369-382` (which already blocks mutation of
   `bulk_send_id`/`import_version`/`source_row_number`). Extend the same
   `previous = type(self).objects.filter(pk=self.pk).values(...)` read to include
   `send_status`, and raise `ValueError` when
   `previous["send_status"] == "sent" and self.send_status != "sent"`, and when
   `previous["send_status"] in SEND_TERMINAL and self.send_status != previous["send_status"]`.
   This catches ORM-object writes originating outside the state module (admin,
   shell, a future careless caller). It is a safety net, not the primary
   mechanism, because it costs one extra query per save and is not atomic.
2. **A whitelist of legal edges** as a module constant, asserted by the state
   module before issuing any `UPDATE`:
   `{("not_started","sending"), ("sending","sent"), ("sending","send_failed"), ("sending","ambiguous")}`.
   The spec's "only the defined forward edges are reachable" scenario is then a
   test over this constant plus a test that no other module in `relay/` writes
   `send_status` (grep-provable: `send_status=` assignment appears only in
   `bulk_v2_send_state.py` and the migration).

**Rejected: a database trigger.** A `BEFORE UPDATE` trigger could enforce the
transition graph including invariant 6 at the storage layer. Rejected because it
requires backend-specific raw SQL (PL/pgSQL on PostgreSQL, a different dialect on
SQLite where the dev/test database runs, per `config/settings.py:87-114`), it is
invisible to `makemigrations` and to model-level tests, and it makes the
migration's reverse operation hand-written rather than automatic — directly
conflicting with the spec's "additive and reversible" requirement. The
compare-and-set already provides atomic, backend-portable transition safety.

**Rejected: a partial unique index enforcing at most one `sending` row per
`BulkSend`** (`UniqueConstraint(fields=["bulk_send"], condition=Q(send_status="sending"))`).
It would be a tidy hard stop on concurrent in-flight attempts and both backends
support it. Rejected because the spec explicitly requires the safety properties
to hold *generally*, "not conditioned on `MAX_ROWS == 1`". A single-in-flight
uniqueness constraint is a canary-shaped artefact that would have to be dropped —
a non-additive schema change — the first time more than one worker is allowed.
Single-writer safety comes from the per-row claim (§4), which generalises without
modification.

### 2.6 What is explicitly untouched

`BulkSendRecipient.status` keeps its `pending | invalid` choices
(`relay/models.py:302-307, 321-325`) and its `CheckConstraint`
`bulk_recipient_valid_status` (`relay/models.py:339-342`) keeps its exact
condition `Q(status__in=["pending", "invalid"])`. The migration (§12) contains no
`AlterField` on `status`, no `RemoveConstraint`, and no re-`AddConstraint` for
that name. The other three existing constraints
(`uniq_bulk_import_source_row`, `bulk_recipient_import_version_gte_1`,
`bulk_recipient_source_row_gte_1`) and both existing indexes
(`bulk_recipient_status_idx`, `bulk_recipient_order_idx`) are likewise absent
from the operations list. Import semantics and send semantics never collide
because they never share a column.

---

## 3. D2 — Attempt modelling: fields on `BulkSendRecipient`, not a `SendAttempt` model

**Decision: fields directly on `BulkSendRecipient` (the nine in §2.1). No
separate attempt table.**

The tradeoff, stated honestly in both directions:

| | Fields on `BulkSendRecipient` (chosen) | Separate `SendAttempt` model (rejected) |
|---|---|---|
| Durability before the outbound call | Identical — one single-row `UPDATE`, committed in its own transaction | Identical — one `INSERT`, committed in its own transaction |
| Attempt numbering | `send_attempt_number`, `F(...) + 1` under the row lock | `attempt_number` column, or `COUNT(*)` — needs its own uniqueness constraint per `(recipient, attempt_number)` to stay monotonic under concurrency |
| Full attempt **history** | **Lost on overwrite** if a recipient is ever attempted twice | **Preserved** — this is the real advantage |
| Schema surface | 9 columns, 6 constraints, 1 index on an existing table | New table + FK + its own constraints + its own index + a consistency invariant *between* the two tables (recipient's `send_status` must agree with its latest attempt row) |
| Review/blast surface in the highest-risk PR | Smaller | Larger, and the cross-table consistency invariant is a new class of bug the field-based model simply cannot have |

**Why the history disadvantage does not bite here:** in this change a recipient
can be attempted **at most once, ever**. `send_failed` and `ambiguous` are both
terminal; there is no automatic retry by construction, and no manual-retry code
path is being built (the command forbids `--retry-ambiguous` and `--force`). So
`send_attempt_number ∈ {0, 1}` for the entire life of this canary, and the
"history" a separate table would preserve is a history of exactly one event that
the recipient row already fully describes: when it started
(`send_started_at`), what its outcome was (`send_status` + `send_error_code`),
and what identifier Doppler returned (`send_message_id`, `send_location`). The
spec's auditability requirement is met exactly, from the database alone.

**Forward path, so this is not a trap.** `send_attempt_number` is the deliberate
migration seam. If a later stage authorises a second attempt per recipient, a
`SendAttempt` model can be added *additively* with an FK to
`BulkSendRecipient` and backfilled row-for-row from the existing columns —
because every column a single attempt row would need already exists with the
right semantics. Nothing designed here has to be undone. Registered as a
named follow-up, not hidden work.

**Secondary record, explicitly not a source of truth.** The three log events
(§11) are append-only in journald and do carry every attempt, so an overwritten
attempt would still be reconstructable operationally. The design does not rely on
this: no invariant, no recovery path, and no test may read a log to determine
state. It is stated only so the residual risk is quantified honestly.

---

## 4. D3 — Single-writer claiming

**Mechanism: `select_for_update(skip_locked=True)` inside `transaction.atomic()`,
followed by a compare-and-set `UPDATE`, committed before returning.** This
mirrors the pattern already proven in this repo at `claim_next_job()`
(`relay/services/jobs.py:72-87`) and re-used at
`relay/management/commands/process_background_jobs.py:48-58`.

```python
# relay/services/bulk_v2_send_state.py

def claim_next_recipient(bulk_send_id: int, *, job_id: int) -> int | None:
    """Atomically move exactly one eligible row not_started -> sending.

    Returns the claimed row pk, or None. The transition is COMMITTED when this
    function returns; no network call has been made.
    """
    now = timezone.now()
    with transaction.atomic():
        row = (
            BulkSendRecipient.objects
            .select_for_update(skip_locked=True)
            .filter(
                bulk_send_id=bulk_send_id,
                status=BulkSendRecipient.STATUS_PENDING,        # invariant 1, layer 1
                send_status=BulkSendRecipient.SEND_NOT_STARTED,
            )
            .order_by("import_version", "source_row_number")    # deterministic
            .values_list("pk", flat=True)
            .first()
        )
        if row is None:
            return None
        updated = (
            BulkSendRecipient.objects
            .filter(
                pk=row,
                status=BulkSendRecipient.STATUS_PENDING,
                send_status=BulkSendRecipient.SEND_NOT_STARTED,  # compare-and-set
            )
            .update(
                send_status=BulkSendRecipient.SEND_SENDING,
                send_started_at=now,
                send_attempt_number=F("send_attempt_number") + 1,
                send_job_id=job_id,
                updated_at=now,
            )
        )
        if updated != 1:
            return None          # lost the race; defensive, unreachable under a real lock
    return row                   # <- commit happened at the end of the with-block
```

Why both the lock **and** the compare-and-set:

- `skip_locked=True` is the *throughput* mechanism: a second worker steps over a
  row another worker holds instead of blocking behind it. It matches the
  established repo pattern, so it will read as idiomatic to a reviewer.
- The `WHERE send_status = 'not_started'` predicate on the `UPDATE` is the
  *correctness* mechanism, and it is backend-independent. This matters
  concretely: the dev/test database is SQLite (`config/settings.py:87-95`,
  `USE_SQLITE` defaults to `DEBUG`), and Django's SQLite backend does not
  advertise `has_select_for_update`, so the row lock is not a guarantee there.
  The correctness argument must not depend on the lock, and it does not.
- Consequence for tests: the two-workers-race scenario follows this repo's
  existing convention for lock-dependent tests —
  `@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")` on a
  `TransactionTestCase` with real threads, exactly as
  `relay/tests/test_bulk_v2_postgresql.py:17-27` already does. The
  compare-and-set semantics are additionally tested backend-independently by
  simulating a stale claim (mutate the row, then attempt the transition).

`.order_by("import_version", "source_row_number")` makes claim order
deterministic and matches the existing `bulk_recipient_order_idx`.

**The commit boundary is a hard design rule**, not a convention: the outbound
Doppler call happens *after* `claim_next_recipient` returns, outside any open
transaction. To make that enforced rather than assumed, `process_bulk_id_v2`
asserts at entry:

```python
if not transaction.get_autocommit():
    raise SendStateError("real send must not run inside an open transaction")
```

This is cheap, testable (`with transaction.atomic(): self.assertRaises(...)`),
and it converts the spec's "no outbound call is ever made against an uncommitted
attempt" scenario from a review promise into a runtime invariant.

---

## 5. D4 — Stale `sending` policy

**A `sending` row is *aged* when `send_started_at < now - STALE_SENDING_AFTER`,
where `STALE_SENDING_AFTER = timedelta(seconds=10 * DOPPLER_RELAY["TIMEOUT"])` =
300 s.**

Anchoring, not a guess: `config/settings.py:140` sets
`DOPPLER_RELAY["TIMEOUT"] = 30`, and `_request` applies it as the `requests`
timeout when the caller supplies none (`doppler_relay.py:244-245`). With
`max_attempts=1` (§9) a single attempt therefore cannot legitimately spend more
than ~30 s in the network call. The 10× multiplier absorbs process scheduling,
DB round-trips, and a slow-but-live worker, so a row past the threshold is
unambiguously abnormal rather than merely slow. Deriving it from the timeout
rather than hard-coding 300 keeps the two coupled if the timeout ever changes.

**It is a module constant in `relay/services/bulk_v2_send_state.py`, not a
Django setting.** Rationale: the spec pins the real-send authorization surface at
exactly six flags. A seventh `BULK_PROCESSING_V2_REAL_SEND_*` setting would blur
"how many knobs authorize a send", and this knob authorizes nothing — it only
changes how a report is worded. It is not operationally urgent to tune, because
crossing it never causes an action.

**What an aged row produces: an operator-visible refusal, and nothing else.**

- No automatic transition. Not back to `not_started`, not forward to anything.
  The row keeps `send_status="sending"` and its original `send_started_at`
  indefinitely, across any number of service restarts.
- No scheduled detector, no cron, no startup hook. Adding a background process
  whose job is to look at `sending` rows creates the one thing the spec forbids:
  a machine with an opinion about unresolved sends. The cheapest correct detector
  is one that only a human can trigger.
- Detection lives in the management command's read-only pre-flight ledger (§6).
  On finding an aged row the command emits
  `bulk_v2_real_send_decision decision=refused code=real_send_stale_sending_present`,
  prints the row's `idempotency_key`, `send_started_at`, age, `send_attempt_number`
  and `send_job_id`, and exits **4** without touching anything.
- The spec's "discoverable without guessing" scenario is satisfied by the two
  columns alone: `send_status` and `send_started_at` are sufficient, with no CSV,
  no log tailing, and no process memory. The runbook records the one-line ORM
  query so an operator can reach the same answer without the command.

For a one-email canary, an aged `sending` row *is* the canary outcome: the single
attempt is unresolved, so the correct behaviour is to refuse everything and hand
it to a human, which is exactly what exit 4 does.

---

## 6. D5 — Crash recovery, concretely

One read-only function is the whole recovery mechanism.

```python
# relay/services/bulk_v2_send_state.py

@dataclass(frozen=True)
class LedgerRow:
    pk: int
    recipient_key: str          # str(idempotency_key)
    domain: str                 # normalized_recipient split on the last "@"
    klass: str                  # see the classification table
    send_status: str
    send_started_at: datetime | None
    age_seconds: float | None
    attempt_number: int
    error_code: str
    message_id: str
    job_id: int | None

@dataclass(frozen=True)
class SendLedger:
    bulk_send_id: int
    rows: tuple[LedgerRow, ...]
    excluded: tuple[LedgerRow, ...]
    eligible: tuple[LedgerRow, ...]
    in_flight: tuple[LedgerRow, ...]
    stale_sending: tuple[LedgerRow, ...]
    terminal_sent: tuple[LedgerRow, ...]
    terminal_failed: tuple[LedgerRow, ...]
    blocked_ambiguous: tuple[LedgerRow, ...]

def describe_send_ledger(bulk_send_id: int, *, now=None) -> SendLedger: ...
```

The query is one statement, no joins, no file access:

```python
BulkSendRecipient.objects.filter(bulk_send_id=bulk_send_id).values(
    "pk", "idempotency_key", "normalized_recipient", "status", "send_status",
    "send_started_at", "sent_at", "send_attempt_number", "send_error_code",
    "send_message_id", "send_job_id",
).order_by("import_version", "source_row_number")
```

Classification, total and mutually exclusive:

| Condition | Class |
|---|---|
| `status == "invalid"` | `excluded` |
| `send_status == "not_started"` | `eligible` |
| `send_status == "sending"` and `age <= STALE_SENDING_AFTER` | `in_flight` |
| `send_status == "sending"` and `age > STALE_SENDING_AFTER` | `stale_sending` |
| `send_status == "sent"` | `terminal_sent` |
| `send_status == "send_failed"` | `terminal_failed` |
| `send_status == "ambiguous"` | `blocked_ambiguous` |

**A restarted worker or command decides what to do next by evaluating exactly
this ordered rule, and nothing else:**

1. `blocked_ambiguous` non-empty → do nothing at all, exit 3. Ambiguity aborts
   the canary regardless of what else is eligible.
2. `stale_sending` non-empty → do nothing, exit 4.
3. `in_flight` non-empty → do nothing, exit 4 with code
   `real_send_in_flight_present`. Another worker may own that row right now, and
   the restarted process has no way to distinguish "live" from "died two seconds
   ago" — so it declines rather than guesses.
4. `eligible` empty → nothing to do, exit **0**, report only. This is the
   already-completed case and it is a *success*, not an error: a retried job or a
   duplicated invocation lands here and is a clean no-op.
5. otherwise → hand `eligible` to the authorization gate and proceed.

The eight crash/restart/concurrency scenarios map onto this rule with no special
cases:

| Scenario | DB state after the event | Rule branch | Result |
|---|---|---|---|
| Worker dies **before** the Doppler call | `sending`, `send_started_at` set (committed in §4) | 3, then 2 once aged | Never re-sent; becomes operator-visible |
| Worker dies **during** the call | `sending` | 3, then 2 once aged | Human evaluates toward `ambiguous` (§10) |
| Doppler accepted, worker died **before** persisting `sent` | `sending` | 3, then 2 once aged | Never reported as `sent` on assumption — the exact case the design exists for |
| Worker dies **after** persisting `sent` | `sent`, `sent_at` set | 4 | Clean no-op, exit 0 |
| `django.service` restart with a row in `sending` | unchanged `sending` | 3/2 | No startup process touches it |
| Two workers race the same row | one `sending`, other claims nothing | §4 | Exactly one Doppler call |
| `BackgroundJob` retried | ledger unchanged by the retry | 4 (or 5 for a still-`not_started` row) | Only `not_started` rows are ever acted on |
| Same job executed twice | ledger unchanged by the second dispatch | 4 / claim returns `None` | At most one Doppler call per row |

**Zero CSV dependency is structural, not disciplinary.** Neither
`describe_send_ledger` nor `claim_next_recipient` nor `process_bulk_id_v2` nor
the management command imports `csv`, opens `bulk.recipients_file`, or references
`BulkImportService`. The spec's "behaviour is identical whether the file is
present, missing, or corrupted" scenario is therefore testable by deleting the
file and re-running: the code has no reference to break.

---

## 7. D6 — Where the double-execution safety boundary actually is

**Decision: the safety boundary is the per-recipient compare-and-set claim (§4).
Job-level locking is defence in depth and the design must not depend on it.**

Why job-level locking is insufficient, stated as three independent gaps rather
than one:

1. **It is bypassable by an existing, in-tree call path.**
   `run_background_job(job_id)` (`relay/services/jobs.py:68-69`) calls
   `run_tracked_job` + `dispatch_background_job` directly, never touching
   `claim_next_job()`'s `select_for_update` (`jobs.py:72-87`). Anything that
   calls it — today or after a future refactor — re-enters dispatch with no lock
   at all.
2. **It protects the wrong object.** A job-level lock can at best prevent two
   simultaneous dispatches of *the same job row*. It cannot prevent a *second job
   row* being created for the same `bulk_send_id`, which is a single
   `BackgroundJob.objects.create(...)` away and would pass every job-level check.
3. **It cannot survive a crash.** `BackgroundJob` has no lease, no heartbeat, and
   no running-timeout anywhere in the codebase. A job left in `state="running"`
   by a killed process is byte-for-byte indistinguishable from one that is
   healthy. Any recovery decision made at the job level is therefore a guess —
   and the proposal's principle 3 is precisely that the job is orchestration, not
   progress.

Only the recipient row carries a fact whose meaning survives the process:
`send_status="sending"` means "an attempt for *this recipient* was durably
recorded", independently of which job, which process, or how many times dispatch
was entered. That is why it is the boundary.

**What the design nevertheless does at the job level, because it is cheap:**

- The new job type is routed through the **locked** claim path, never through
  `run_background_job`. The management command creates the `BackgroundJob` row
  and then claims its own job with the same row-locked pattern already used for
  type-filtered claiming in `process_background_jobs.py:48-58`
  (`select_for_update(skip_locked=True).filter(pk=..., state=STATE_QUEUED)`),
  then calls `run_claimed_job(job)`. This yields an auditable job row, a job-level
  lock, and synchronous operator-visible execution in one shell invocation.
- Before creating the job, the command refuses if a `queued` or `running` job of
  the new type already exists for that `BulkSend`
  (`real_send_job_already_present`, exit 4).

**Explicitly rejected: adding a lease / heartbeat / `locked_at` /
running-timeout to `BackgroundJob`** (proposal open question 3). Rejected on two
grounds. First, `BackgroundJob` is shared V1 orchestration machinery; changing
its lifecycle semantics is precisely the V1 modification this initiative forbids,
and it would put V1's `bulk_send` and `post_report` jobs at risk for a V2
benefit. Second, it would buy nothing: a lease answers "is a job still owned",
which is not the question that governs whether a Doppler call may be made. The
question that governs it is "has an attempt for this recipient already been
durably recorded", and that is answered by the recipient row alone. It would also
grow PR2 past a reviewable budget — the exact outcome the proposal's delivery
section warns against.

---

## 8. D7 — Management command behaviour

**Module:** `relay/management/commands/bulk_v2_real_send.py`.
**Invocation:** `python manage.py bulk_v2_real_send --bulk-send-id <int> [--dry-run]`.

### 8.1 Argument surface — complete and closed

| Argument | Type | Notes |
|---|---|---|
| `--bulk-send-id` | `int`, `required=True` | No default. No `--latest`, no `--all`, no positional fallback. |
| `--dry-run` | `store_true` | Runs every check and the ledger report, then stops **before** the claim. Strictly more restrictive than the default; it can never cause a send. |

**Nothing else exists.** No `--retry-ambiguous`, no `--force`, no `--yes`, no
recipient argument of any kind (wildcard or literal), no `--template`, no
`--file`, no `--limit`. The spec's "no override flags exist in the command's
interface" scenario is tested by asserting the exact set of
`parser._actions` dest names equals `{"bulk_send_id", "dry_run"}` plus argparse's
built-ins and Django's `BaseCommand` defaults.

The command never imports `csv`, never touches `bulk.recipients_file`, and never
imports `BulkImportService`.

### 8.2 Ordered checks — first failure wins, each with a distinct exit code

| # | Check | Refusal code | Exit | Side effects on failure |
|---|---|---|---|---|
| 1 | `--bulk-send-id` present and integral | argparse error | 2 | none — fails before any DB read, satisfying "fails before evaluating the gate or reading any row" |
| 2 | `settings.BULK_PROCESSING_V2_REAL_SEND_ENABLED is True` | `real_send_disabled` | 5 | none — **zero DB queries** executed |
| 3 | `BulkSend` with that pk exists | `real_send_bulk_not_found` | 2 | one read |
| 4 | `bulk.engine_version == BulkSend.ENGINE_V2` | `real_send_engine_not_v2` | 2 | none |
| 5 | `bulk.import_status in {ready, ready_with_errors}` | `real_send_import_not_ready` | 2 | none |
| 6 | no `queued`/`running` job of type `bulk_send_v2_real` for this bulk | `real_send_job_already_present` | 4 | none |
| 7 | ledger read (`describe_send_ledger`) | — | — | read-only |
| 8 | `blocked_ambiguous` is empty | `real_send_ambiguous_present` | **3** | none — see §8.3 |
| 9 | `stale_sending` is empty | `real_send_stale_sending_present` | 4 | none |
| 10 | `in_flight` is empty | `real_send_in_flight_present` | 4 | none |
| 11 | `eligible` is non-empty | `real_send_nothing_to_send` | **0** | none — success no-op |
| 12 | `evaluate_real_send(...)` returns `allowed=True` | the decision's own code | 5 | none |
| 13 | `--dry-run` | `real_send_dry_run` | **0** | none — nothing is claimed |
| 14 | execute | `real_send_allowed` | 0 on completion | creates the job, claims, calls Doppler once |

Every one of checks 2–13 emits exactly one
`bulk_v2_real_send_decision` INFO event carrying the code, and terminates via
`CommandError(message, returncode=N)` (Django's `CommandError` accepts
`returncode`), so an operator gets both a journald record and a shell-inspectable
status.

Notes on the ordering, since the order is itself a decision:

- **The kill switch is check 2, before any database access.** It duplicates a
  check that `evaluate_real_send` also performs at step 12. The duplication is
  deliberate: the spec requires the kill switch to stop a send "regardless of any
  other flag or in-flight state", and the strongest form of that is a refusal
  that reads nothing and can therefore fail in no other way.
- **Structural checks (3–5) precede the ledger** because there is no point
  classifying rows of a bulk that is not a V2 bulk or was never imported.
- **The ledger (7–11) precedes the gate (12)** for a mechanical reason, not a
  preference: `evaluate_real_send` is a pure function (§9 of the authorization
  spec, and §13 here) that takes the eligible row count and the eligible
  recipients' domains as *inputs*. Those inputs only exist after the ledger is
  read. Reversing the order would force the gate to query the ORM, destroying its
  purity and its PR1-testability.
- **`nothing_to_send` exits 0, not non-zero.** A retried job, a duplicated
  invocation, or a re-run after a successful canary all land here. Treating an
  idempotent no-op as a failure would train operators to re-run with escalating
  force, which is the behaviour this whole change exists to prevent.

Exit-code vocabulary, chosen so a shell can branch without parsing text:

| Code | Meaning |
|---|---|
| 0 | Success, dry run, or clean no-op |
| 2 | Structural/precondition problem (bad args, wrong engine, not imported) |
| 3 | **Ambiguity present — human resolution required, canary aborted** |
| 4 | In-flight, stale, or duplicate-job state — human inspection required |
| 5 | Authorization refused (kill switch or any allowlist dimension) |

### 8.3 Behaviour on encountering `ambiguous`

Exactly as the table says, and worth restating because it is the spec's hardest
scenario:

- The command **reports** the ambiguous row — `idempotency_key`, `send_started_at`,
  `send_attempt_number`, `send_error_code`, `send_message_id`, `send_location`,
  `send_job_id` — to stdout and to the `bulk_v2_real_send_decision` event.
- The command **does not touch it**: no re-send, no retry, no state change, no
  clearing of any field.
- The command **refuses to process anything else in the same invocation**, and
  exits 3. It does not "skip and continue". With `MAX_ROWS == 1` there is nothing
  else to process anyway, but the general rule is the safe one and it is what the
  spec's "the canary MUST be treated as aborted" requires.
- There is no code path anywhere in the change that transitions a row *out of*
  `ambiguous`. Resolution is a human writing to the database under the runbook,
  after evidence (§10) — not a feature.

---

## 9. D8 — Authorization gate shape (`evaluate_real_send`)

New module `relay/services/bulk_v2_real_send.py`. **Pure function: no ORM, no
`settings` import, no I/O, no HTTP, no filesystem.** Every input is passed in.
This is what makes it fully testable in PR1, before any send-capable code exists.

```python
@dataclass(frozen=True)
class RealSendDecision:
    allowed: bool
    code: str
    message: str

def evaluate_real_send(
    *,
    real_send_enabled: bool,
    user_allowlist: str | Iterable[Any],
    request_allowlist: str | Iterable[Any],
    template_allowlist: str | Iterable[Any],
    recipient_domain_allowlist: str | Iterable[Any],
    max_rows: Any,
    user_id: int | None,
    client_request_id: str,
    template_id: str,
    recipient_domains: Iterable[str],
    eligible_row_count: int,
) -> RealSendDecision: ...
```

Independence from `evaluate_canary`, satisfying the spec literally:

- Distinct function, distinct module, distinct parameter list (11 keyword
  parameters, none of which is `send_now`, `scheduled_at`, `import_only`,
  `engine_enabled`, `canary_enabled`, or `allow_external_template_lookup`).
- Distinct return type: `RealSendDecision`, **not** `CanaryDecision`. Structurally
  similar by coincidence of good taste, not by shared contract; neither is
  assignable to the other and no code converts between them.
- Neither function calls the other, in either direction, on any path.
- The only reuse is `normalize_allowlist`
  (`relay/services/bulk_v2_canary.py:16-39`), which contains no canary policy —
  it is a comma-string parser that rejects empties, duplicates, wildcards, and
  non-positive integers. Its existing `"canary_config_invalid"` sentinel is
  treated as an opaque error signal and translated to
  `real_send_config_invalid`; the canary string is never propagated outward.

**All refusal codes are prefixed `real_send_`** so no operator, grep, or alert
rule can confuse a real-send refusal with a canary refusal:
`real_send_disabled`, `real_send_config_invalid`, `real_send_user_allowlist_empty`,
`real_send_request_allowlist_empty`, `real_send_template_allowlist_empty`,
`real_send_domain_allowlist_empty`, `real_send_request_id_required`,
`real_send_request_not_allowed`, `real_send_user_not_allowed`,
`real_send_template_not_allowed`, `real_send_domain_not_allowed`,
`real_send_row_count_empty`, `real_send_row_limit_exceeded`. Success:
`real_send_allowed`.

**`MAX_ROWS` is validated as exactly `1`, not `<= 1`.** Any other value —
including `0`, `2`, or a non-integer — returns `real_send_config_invalid`. This
makes the one-email ceiling un-widenable by environment alone; raising it
requires a code change and a review. The precedent is in this repo:
`ops/td02c_settings_gate.py:67` hard-codes `max_rows != 20` for the import canary
for exactly the same reason. `BULK_PROCESSING_V2_CANARY_MAX_ROWS` is never read
by this module — the name does not appear in the file.

Template authorization is keyed on `template_id` only
(`"templateId": str(template_id)` is the sole template identifier Doppler
receives, `doppler_relay.py:614`). `template_name` is not a parameter of
`evaluate_real_send`; it is absent from the module entirely.

Recipient authorization is by domain: the command derives each eligible row's
domain from `normalized_recipient` (lowercased, the segment after the final
`"@"`) and passes the tuple in. No email address appears in settings, in the
command, in the gate, or in any test fixture; test fixtures use
`example.com`-style domains supplied as test parameters.

Settings added to `config/settings.py`, following the existing `env(...)` idiom
at lines 155-184, all defaulting to inert values:

```python
BULK_PROCESSING_V2_REAL_SEND_ENABLED = env.bool("BULK_PROCESSING_V2_REAL_SEND_ENABLED", default=False)
BULK_PROCESSING_V2_REAL_SEND_USER_IDS = env("BULK_PROCESSING_V2_REAL_SEND_USER_IDS", default="")
BULK_PROCESSING_V2_REAL_SEND_REQUEST_IDS = env("BULK_PROCESSING_V2_REAL_SEND_REQUEST_IDS", default="")
BULK_PROCESSING_V2_REAL_SEND_TEMPLATE_IDS = env("BULK_PROCESSING_V2_REAL_SEND_TEMPLATE_IDS", default="")
BULK_PROCESSING_V2_REAL_SEND_RECIPIENT_DOMAINS = env("BULK_PROCESSING_V2_REAL_SEND_RECIPIENT_DOMAINS", default="")
BULK_PROCESSING_V2_REAL_SEND_MAX_ROWS = env.int("BULK_PROCESSING_V2_REAL_SEND_MAX_ROWS", default=1)
```

---

## 10. D9 — Doppler single-attempt call path

**Decision: add a constructor keyword `max_attempts: int = 3` to
`DopplerRelayClient`, stored on the instance, and change exactly one line inside
`_request()` — line 227, `max_retries = 3` — to read `max_retries = self.max_attempts`.
V2 obtains its client from a named factory that passes `max_attempts=1` and calls
the *existing, unmodified* `send_template_message`.**

```python
# relay/services/doppler_relay.py  (two-line change)
def __init__(self, ..., max_attempts: int = 3):
    ...
    self.max_attempts = max(int(max_attempts), 1)

def _request(self, method, path, **kwargs):
    url = self._url(path)
    max_retries = self.max_attempts        # was: max_retries = 3
    ...                                    # loop body byte-identical

# relay/services/bulk_v2_send.py
def build_single_attempt_client() -> DopplerRelayClient:
    return DopplerRelayClient(max_attempts=1)
```

Verification that V1 retry behaviour is unchanged: every existing V1 construction
site instantiates `DopplerRelayClient()` with no `max_attempts`, so
`self.max_attempts == 3` and the loop at `doppler_relay.py:231-293` runs with the
same bound, the same `min(0.8 * 2**retry_count, 8)` backoff, the same warning and
error logging, and the same terminal raise. Mandatory tests: (a) the default is
`3`; (b) a mocked transport that always raises is invoked exactly 3 times for a
default client and exactly 1 time for a `max_attempts=1` client; (c) the whole
existing V1 suite passes untouched.

With `max_attempts=1` the loop enters once; on success it returns immediately, on
failure `retry_count` becomes 1, `1 < 1` is false, the `else` branch logs and
`break`s, and `last_error` is raised — **no `time.sleep`, no second socket
write**. Exactly one HTTP attempt, which is the entire requirement.

Because the safety property now lives on the instance rather than at the call
site, it is asserted where it matters, immediately before the call:

```python
if getattr(client, "max_attempts", None) != 1:
    raise SendStateError("real send requires a single-attempt Doppler client")
```

Rejected alternatives, with the reason each is worse:

- **A new `send_template_message_once()` + `_request_once()`.** Superficially the
  cleanest separation, but `send_template_message` calls `_request` at line 694
  from the *middle* of a ~190-line method whose first ~140 lines build the
  payload. A `_once` variant therefore needs either (i) a duplicated payload
  builder — which guarantees eventual divergence between what V1 sends and what
  V2 sends, defeating the point of a canary that is supposed to prove V2 sends
  the same thing; or (ii) a pure extraction of lines 570-691 into a shared
  builder — a much larger, riskier edit to a V1 code path than the one-line
  change chosen, requiring a golden-payload characterization test to be safe.
  Both are strictly more V1 surface for strictly less benefit.
- **A thin wrapper function outside the client.** Rejected by the proposal and
  confirmed here: it would have to re-derive `base_url` (`_url`, line 159-160),
  the `Authorization`/`User-Agent`/`Accept` headers (lines 106-110), the timeout
  (line 140), and the `_raise_for_api` error typing (lines 162-222). That is
  duplicated integration, and a credential-handling duplicate at that.
- **A `SingleAttemptDopplerClient` subclass overriding `_request`.** Rejected:
  the override is invisible at the call site, and a future edit to `_request`
  silently changes the subclass's semantics with no compiler or test signal.

### 10.1 Outcome classification — the exact mapping from what happens to `send_status`

Grounded in the real error surface of `_request`/`_raise_for_api`, not in an
idealised one:

| Observation at the call site | `send_status` | `send_error_code` |
|---|---|---|
| Returns the mapped dict (HTTP 2xx, body parsed) | `sent` | `""` |
| `DopplerRelayError` with `status == 402` and Doppler `errorCode == 1` (the deliveries-quota rejection handled at `doppler_relay.py:197-206`) | `send_failed` | `doppler_quota_exceeded` |
| `DopplerRelayError` with `status` in 400–499 except 408 and 429 | `send_failed` | `doppler_http_{status}` |
| `DopplerRelayError` with `status` in {408, 429} or `>= 500` | `ambiguous` | `doppler_http_{status}` |
| `DopplerRelayError` with `status` absent/`None` | `ambiguous` | `doppler_error_no_status` |
| `ValueError`/`JSONDecodeError` from `response.json()` (line 701) | `ambiguous` | `response_unparseable` |
| `requests.Timeout` / `ConnectionError` / any `RequestException` | `ambiguous` | `timeout` / `connection_error` / `request_exception` |
| **Any other exception**, including `AttributeError` — see below | `ambiguous` | `dispatch_exception` |
| Exception raised *before* the socket write (payload validation, e.g. `ValueError("from_email es requerido")` at line 599) | `send_failed` | `payload_invalid` |

Two grounded details that shape this table:

1. **A definitive 4xx is a definitive rejection; a 5xx is not.** A 500/502/503
   can be a gateway that never reached the mail pipeline, or a failure *after*
   acceptance. Fail-safe therefore classifies it `ambiguous`, accepting that a
   transient upstream blip aborts the canary rather than risking a duplicate.
   `429` is treated as ambiguous rather than a clean rate-limit failure because
   the client has no parser proving it was rejected pre-acceptance; `402` with
   `errorCode == 1` *does* have such a parser (`doppler_relay.py:197`) and is
   therefore classified definitively.
2. **The V2 call site must catch bare `Exception`, not just
   `DopplerRelayError`/`RequestException`.** Discovered while designing this:
   `_request`'s terminal wrapper at `doppler_relay.py:286-293` evaluates
   `status=getattr(last_error, 'response', {}).status_code`. For a
   `requests.Timeout` or `ConnectionError`, `last_error.response` **exists and is
   `None`**, so `getattr` returns `None` (the default is only used for a *missing*
   attribute) and `None.status_code` raises `AttributeError` — meaning that on a
   network error `_request` raises `AttributeError`, not `DopplerRelayError`.
   This is a pre-existing V1 defect. **It is not fixed here** (V1 stays
   untouched); it is registered as separate technical debt, and the V2 design
   absorbs it by classifying any unexpected exception raised after dispatch as
   `ambiguous`. Designing around the *actual* error surface rather than the
   documented one is the difference between a canary that degrades safely and one
   that crashes with a row stuck in `sending`.

The terminal transition is written with the compare-and-set of §2.5, in its own
transaction, after the call returns or raises — never inside a transaction that
was open during the call.

---

## 11. D10 — Evidence required to resolve `ambiguous`, and what is *not* verified

### 11.1 What the code actually produces today

From `send_template_message` (`doppler_relay.py:693-726`), read directly:

```python
result = response.json()
result["_location"] = response.headers.get("Location")
message_id = result.get("message_id") or (
    result.get("_location", "").split("/")[-1] if result.get("_location") else ""
)
```

So the only identifiers obtainable on accept are:

1. a `message_id` key, **if** Doppler's JSON body happens to contain one; otherwise
2. the trailing path segment of the `Location` response header; otherwise
3. the empty string.

The client can query afterwards via `get_delivery(account_id, delivery_id)` →
`GET /accounts/{id}/deliveries/{delivery_id}` (`doppler_relay.py:752-753`),
`list_deliveries(account_id, from_iso=..., to_iso=...)` (736-750), and
`list_events(...)` (765-779).

### 11.2 The unverified assumption, stated as such

**It is NOT established that the identifier returned on accept is the same
identifier accepted by `GET /accounts/{id}/deliveries/{delivery_id}`, nor that it
appears in a `list_deliveries` or `list_events` record in a form that
unambiguously identifies one attempt.** Nothing in the repository contains a
captured response body or `Location` header from a real
`POST /accounts/{id}/templates/{id}/message`, this phase has no live Doppler
access, and the vendor documentation review recorded in #354 did not establish
the correspondence. Assuming it would be exactly the kind of "probably fine" the
proposal's non-negotiable floor forbids.

Therefore the design **persists both artefacts and correlates neither
automatically**:

- `send_message_id` stores whatever the existing extraction yields.
- `send_location` stores the raw `Location` header, because a full URL is
  strictly more useful for a later manual query than a trailing segment whose
  meaning is unconfirmed, and it costs one column.
- **No code in this change ever queries Doppler to resolve an `ambiguous` row.**
  There is no reconciliation command, no `--retry-ambiguous`, and no automatic
  `ambiguous -> sent` path anywhere.

### 11.3 The floor, which is the actual designed behaviour

An `ambiguous` row stays `ambiguous`. The canary is declared **aborted**. No
re-send occurs — not automatic, not manual, not "just this once". Every consumer
of the state machine (the command, the worker, the ledger) already implements
this: §8.2 check 8 exits 3 and processes nothing.

This means **the design is correct whether or not the correlation turns out to
work.** Correlation is not load-bearing for safety; it is load-bearing only for an
operator's ability to *close out* an ambiguous outcome with a positive finding
instead of an unresolved one.

### 11.4 Named prerequisite for (B), explicitly out of scope for apply

Before the canary's (B) execution phase, an authorized operator must perform a
one-off verification — **not part of `sdd-apply`, not a task in this change**:

1. Capture, from one real accepted `send_template_message` call (V1's existing
   path is sufficient and already sends production mail), the exact HTTP response:
   status, full `Location` header, and full JSON body.
2. Confirm which of `message_id` / `Location`-trailing-segment is actually
   populated.
3. Confirm that `get_delivery(account_id, <that identifier>)` returns a record,
   or that the identifier appears in a `list_deliveries` / `list_events` result
   over the surrounding time window in a way that identifies exactly one message.

If step 3 fails, (B) may still proceed — the canary simply runs with the
understanding that an `ambiguous` outcome would be permanently unresolvable and
would therefore end the canary in an abort. That is already the designed
behaviour; nothing changes structurally.

### 11.5 A clean 2xx with an empty identifier is `sent`, not `ambiguous`

Reading the spec's "missing correlation evidence blocks a `sent` classification"
scenario precisely: it forbids classifying an **uncertain** outcome as `sent` on
the basis of an unproven identifier. An HTTP 2xx that parsed successfully is not
uncertain — acceptance is proven by the status code, independently of any
identifier. So:

- 2xx + parsed body → `sent`, even if `send_message_id` is `""`.
- 2xx + unparseable body → `ambiguous` (`response_unparseable`) — we cannot even
  confirm we read Doppler's answer.

An empty identifier does not corrupt the state machine; it fails the canary at the
*evidence* level instead. The runbook's PASS criteria for (B) require a non-empty
`send_message_id`, so a `sent` row with no identifier is a canary that did not
produce the evidence it existed to produce, and is reported as such — a
truthful state plus a failed success criterion, rather than a lie in either
direction.

---

## 12. D11 — Structured logging and journald

### 12.1 The `LOGGING` dict (design snippet, applied in PR1)

Appended to `config/settings.py`:

```python
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "relay_kv": {"format": "%(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "relay_stderr": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "level": "INFO",
            "formatter": "relay_kv",
        },
    },
    "loggers": {
        "relay": {
            "handlers": ["relay_stderr"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
```

Why each element:

- **`"version": 1` + `"disable_existing_loggers": False`** — Django applies its
  own `DEFAULT_LOGGING` first and then this dict on top; with
  `disable_existing_loggers` false, the `django`, `django.server`, and
  `django.request` loggers configured by the default keep their handlers, levels,
  and filters. This dict defines **no `root` key and no `django` logger**, so
  nothing outside the `relay` tree changes. This additivity claim is not left as
  an assertion: PR1 must include a test that captures
  `logging.getLogger("django.request").level` / `.handlers` /
  `.propagate` and `logging.getLogger("django").level` before and after and
  asserts they are unchanged, plus a test that `logging.getLogger("relay.x")`
  has effective level `INFO`.
- **One logger, `"relay"`.** Every module under `relay/` uses
  `logging.getLogger(__name__)` (e.g. `relay/services/jobs.py:15`,
  `relay/api.py:36` → `relay.api`), so a single parent covers the whole tree with
  no per-module configuration.
- **`StreamHandler` to `sys.stderr`** — reuses the documented
  gunicorn stderr → systemd journal → `journalctl -u django.service` pipeline
  (`DEPLOY.md`). No new destination, no file handler, no rotation to own, no
  syslog socket.
- **No `%(asctime)s`.** journald timestamps every record; adding one duplicates
  it and makes `journalctl -o short-iso` output confusing.
- **`propagate: False`** prevents double emission if a root handler is ever
  added later.

This is the whole of PR1's observability change, and it is verifiable with zero
send-capable code — the spec's explicit PR1-testability requirement.

### 12.2 Event format

Flat `key=value` pairs, space-separated, event name as the first token, matching
the two log lines the Stage 1 evidence procedure already greps
(`bulk_v2_canary decision=... request=... rows=... external_calls=0`,
`relay/api.py:450-455`; `bulk_v2_import bulk_id=... engine=v2 ...`,
`relay/api.py:562-572`). **JSON was rejected**: nothing in this deployment
ingests structured journald fields, and switching format mid-initiative would
break the existing grep-based evidence procedure for no gain.

| Event | When | Fields |
|---|---|---|
| `bulk_v2_real_send_decision` | every `evaluate_real_send` evaluation **and** every command-level refusal (§8.2 checks 2–13) | `decision` (`allowed`\|`refused`), `code`, `bulk_send_id`, `request=<sha256(client_request_id)[:12]>`, `eligible_rows`, `max_rows`, `at` |
| `bulk_v2_real_send_attempt` | after the `sending` transition is **committed**, immediately before the outbound call | `bulk_send_id`, `recipient_key=<idempotency_key>`, `recipient_domain`, `job_id`, `attempt_number`, `send_started_at` |
| `bulk_v2_real_send_result` | after the terminal/ambiguous transition is committed | `bulk_send_id`, `recipient_key`, `recipient_domain`, `job_id`, `attempt_number`, `result` (`sent`\|`send_failed`\|`ambiguous`), `error_code`, `message_id`, `location`, `send_started_at`, `finished_at`, `duration_ms` |

The attempt and result events carry an identical correlation quadruple
(`bulk_send_id`, `recipient_key`, `job_id`, `attempt_number`), which is exactly
what the spec's "correlated to the same ... that were persisted in the `sending`
transition" scenario requires. `result` is an explicit field value, so
`ambiguous` is never conflated with `send_failed` by a scanning operator —
`result=ambiguous` and `result=send_failed` are distinct literal tokens.

The `request` fingerprint reuses the existing `sha256(client_request_id)[:12]`
convention (`relay/api.py:450-455`, `ops/README.md:210-213`) rather than
inventing a second one.

### 12.3 Redaction — coverage of the forbidden list

| Forbidden by spec | How it is impossible here |
|---|---|
| API key / raw token | No event field derives from `settings.DOPPLER_RELAY`. Critically, **`DopplerRelayError.payload` is never logged and never persisted** — `doppler_relay.py:185-194` puts `request_headers` (containing `Authorization: <scheme> <API_KEY>`) and `request_body` into it. Only `exc.status` (an int) and a classified `error_code` reach a log line. |
| Full recipient address | The recipient is identified by `recipient_key` (`idempotency_key`, an opaque UUID) plus `recipient_domain` (an already-allowlisted, non-identifying value). Neither the local-part nor the full address appears in any of the three events. |
| Message body | Never read into the send path's log call; `send_error_message` is stored in the DB but is not a logged field. |
| CSV content | The send path has no reference to `recipients_file` at all. |
| Sensitive template variables | `payload` / `variables` are never read by `bulk_v2_send.py` or by the logging helper. |

Enforcement is a test, not a promise: assert that a captured
`bulk_v2_real_send_result` record's formatted output contains none of the
recipient's local-part, none of the payload values, and not
`settings.DOPPLER_RELAY["API_KEY"]` (set to a distinctive sentinel in the test).

---

## 13. D12 — Migration design

**One migration**, following this repo's timestamp naming convention (as with
`20260514150001_bulk_processing_v2_ledger.py`):

`relay/migrations/20260808120000_bulk_v2_real_send_state.py`, with
`dependencies = [("relay", "20260514150001_bulk_processing_v2_ledger")]`.

Operations, in order — **every one additive**:

1. `AddField` × 9 on `bulksendrecipient` (§2.1). Every column is either nullable
   or has a scalar default; none is `NOT NULL` without a default, so PostgreSQL 11+
   adds them without a table rewrite.
2. `AddIndex` `bulk_recipient_send_status_idx`.
3. `AddConstraint` × 6 (§2.4), all with new names prefixed
   `bulk_recipient_send_*` / `bulk_recipient_invalid_never_sends`.
4. `AlterField` on `backgroundjob.job_type` widening `choices` with
   `("bulk_send_v2_real", "Bulk send V2 real")`.

**Reversibility.** Every operation has an automatic Django inverse
(`RemoveField`, `RemoveIndex`, `RemoveConstraint`, `AlterField` back). There is no
`RunPython`, no `RunSQL`, no data migration, and therefore nothing hand-written or
lossy in the reverse direction. `python manage.py migrate relay
20260514150001_bulk_processing_v2_ledger` restores the previous schema exactly.

**Zero impact on V1.** The operations list contains no reference to
`emailmessage`, `delivery`, `attachment`, `useremailconfig`, or `bulksend`. V1's
send pipeline reads none of the new columns.

**Zero impact on the V2 import path.** `bulk_import.py`'s occurrence persistence
uses explicit field lists on `bulk_create`; the nine new columns take their
declared defaults (`not_started`, `0`, `NULL`, `NULL`, `""`, `""`, `""`, `""`,
`NULL`), which satisfy all six new constraints for both `status="pending"` and
`status="invalid"` rows (§2.4). Existing production rows receive the same shape
from the `AddField` defaults, so the subsequent `ADD CONSTRAINT ... CHECK`
validation scan cannot fail on legacy data.

**Two operational notes that belong in the runbook, not in a surprise:**

- `AlterField` on `job_type` is a Django-level `choices` change only. `job_type`
  is `CharField(max_length=32)` with **no** database `CHECK`
  (`relay/models.py:279`), so this operation emits no meaningful DDL. The new
  value `"bulk_send_v2_real"` is 17 characters, well inside `max_length=32`.
- On PostgreSQL, `ADD CONSTRAINT ... CHECK` takes an `ACCESS EXCLUSIVE` lock and
  scans the table. Under the Stage 1 disposition rule
  (`(jobs, v2, ledger) == (0, 0, 0)` before every deployment) `BulkSendRecipient`
  is empty at deploy time, so the scan is instantaneous. Stated so nobody
  discovers it during a future non-empty deploy.

---

## 14. D13 — PR1 / PR2 boundary as a binding design decision

### PR1 — safe scaffolding. **Contains zero code capable of reaching Doppler.**

| Path | Nature |
|---|---|
| `config/settings.py` | `LOGGING` dict (§12.1) + the six `BULK_PROCESSING_V2_REAL_SEND_*` settings, all defaulting off/empty, `MAX_ROWS` default `1` |
| `relay/services/bulk_v2_real_send.py` (new) | `RealSendDecision` + `evaluate_real_send` — pure function, no ORM, no `settings`, no I/O |
| `relay/tests/test_bulk_v2_real_send_gate.py` (new) | every allowlist dimension, kill switch, `MAX_ROWS != 1` rejection, structural independence from `evaluate_canary` |
| `relay/tests/test_logging_config.py` (new) | `relay` INFO reaches the handler; Django's own loggers unchanged |
| `.env.example` | documents the six flags, all inert |

**The mechanical review criterion for PR1**, checkable by grep on the diff: it
contains no occurrence of `doppler_relay`, `DopplerRelayClient`, `requests`,
`send_template_message`, `BackgroundJob`, `BulkSendRecipient`, `migrations`, or
`management/commands`. If any appears, the boundary has been violated.

### PR2 — the send engine. First Doppler-capable path; still zero execution.

| Path | Nature |
|---|---|
| `relay/models.py` | 9 fields + 1 index + 6 constraints on `BulkSendRecipient`; `TYPE_BULK_SEND_V2_REAL` on `BackgroundJob`; the `save()` terminal-state guard (§2.5) |
| `relay/migrations/20260808120000_bulk_v2_real_send_state.py` (new) | §13 |
| `relay/services/bulk_v2_send_state.py` (new) | `claim_next_recipient`, `mark_sent`, `mark_send_failed`, `mark_ambiguous`, `describe_send_ledger`, `SendStateTransitionError`, `STALE_SENDING_AFTER`, the legal-edge whitelist |
| `relay/services/bulk_v2_send.py` (new) | `process_bulk_id_v2`, `build_single_attempt_client`, the outcome classifier (§10.1), the three log events |
| `relay/services/doppler_relay.py` | **two lines**: `max_attempts` kwarg in `__init__`, `max_retries = self.max_attempts` in `_request` |
| `relay/services/jobs.py` | one additive `elif` branch in `dispatch_background_job`, with a function-local import of `process_bulk_id_v2` so the existing module-level `process_bulk_id` import line is untouched |
| `relay/management/commands/bulk_v2_real_send.py` (new) | §8 |
| `relay/tests/test_bulk_v2_send_state.py` (new) | the six invariants, the transition graph, terminal immutability |
| `relay/tests/test_bulk_v2_real_send_command.py` (new) | the 14 ordered checks, exit codes, absent-argument surface, CSV-independence |
| `relay/tests/test_bulk_v2_real_send_postgresql.py` (new) | the two-worker race, `@skipUnless(connection.vendor == "postgresql")` + `TransactionTestCase` + threads, per `test_bulk_v2_postgresql.py:17-27` |
| `relay/tests/test_doppler_single_attempt.py` (new) | default `max_attempts == 3`, exactly one transport call at `1`, V1 loop unchanged |
| `ops/README.md` | the real-send runbook, **written, never executed** |

**Refinement of the proposal, deliberately more conservative:** the proposal
listed `relay/services/bulk_processing.py` as "added to" for `process_bulk_id_v2`.
This design places `process_bulk_id_v2` in a **new module**
(`relay/services/bulk_v2_send.py`) instead, leaving `bulk_processing.py` with a
zero-line diff. The legacy guard at `bulk_processing.py:39-42` and V1's send file
are then untouched by inspection rather than by argument. Same capability, strictly
smaller V1 surface.

### "Zero execution during apply" — what makes it true

After both PRs land, a real send requires **all** of:
`BULK_PROCESSING_V2_REAL_SEND_ENABLED=True`, four non-empty allowlists that all
match a specific `BulkSend`, an operator with shell access on the host, and an
explicit `manage.py bulk_v2_real_send --bulk-send-id N` invocation. Apply
produces the code with the flag defaulting `False` and every allowlist empty, so
step 12 of §8.2 refuses unconditionally. No test performs a real HTTP call: all
Doppler interaction in tests is mocked at `DopplerRelayClient.session.request` or
`_request`, and a test asserts that no test in the suite constructs a client
against a non-mocked transport.

**Completing (A) is never authorization for (B).** (B) — activating flags,
choosing the concrete user / `client_request_id` / template / recipient domain,
running the command, capturing evidence, closing the flags — requires separate,
explicit, later owner authorization that does not exist and is not requested here.

---

## 15. Traceability: spec requirement → design section

| Capability / Requirement | Design |
|---|---|
| Independent gate, no shared state or fallback | §9 |
| Six flags and a single kill switch | §9, §8.2 check 2 |
| Template authorization by `template_id` only | §9 |
| Recipient authorization by domain, no hardcoded address | §9 |
| `MAX_ROWS` fixed at 1, independent of the canary limit | §9 |
| Command requires an explicit `bulk_send_id` | §8.1, §8.2 check 1 |
| Command fails closed on engine/gate/allowlist mismatch | §8.2 checks 4, 12 |
| Command forbids retry/force/wildcard, never reads the CSV | §8.1, §6 |
| Canary scope: one email, no scheduling, no concurrency, no auto-retry | §9, §8.1, §3 |
| Completing the capability is never authorization | §14 |
| Dedicated `send_status`, independent of import `status` | §2.1, §2.6 |
| `status=invalid` can never enter the send machine | §2.3 #1, §2.4, §4 |
| Valid forward-only transition graph; `sent` immutable | §2.5 |
| Required companion data per state | §2.3, §2.4 |
| Attempt persisted durably before any outbound call | §4 |
| Single-writer claiming | §4 |
| Terminal/in-flight rows immune to job retry and double execution | §6, §7 |
| Stale `sending` never auto-resolves | §5 |
| Crash recovery from persisted state alone, zero CSV dependency | §6 |
| Ambiguous requires human resolution with correlatable evidence | §11 |
| Attempt modelling bound by functional requirements | §3 |
| Correlatable Doppler identifier must be persisted | §11.1, §11.2 |
| Migration additive and reversible | §13 |
| INFO `relay` logs reach journald | §12.1 |
| Decision / attempt / result events | §12.2 |
| Forbidden content never logged | §12.3 |

---

## 16. Explicitly out of scope — do not design or implement here

- Any change to V1: `EmailMessage`, `Delivery`, `process_bulk_id`,
  `process_bulk_template_send`, `bulk_processing.py` (zero diff, §14).
- Fixing V1's CSV-re-reading retry, and fixing the `_request` terminal-wrapper
  `AttributeError` found in §10.1. Both registered as separate technical debt.
- Any lease / heartbeat / running-timeout on `BackgroundJob` (§7).
- A reconciliation or inspection command for `ambiguous` rows (§11.2). The ledger
  ORM snippet in the runbook is the mechanism.
- V2 scheduled send; V2 batch real-send above one recipient; V2 as default engine;
  V1 deprecation; automatic send triggered by import.
- Raising `BULK_PROCESSING_V2_CANARY_MAX_ROWS` (stays 20, import-only).
- Any real send, flag activation, or production mutation during `sdd-apply`.

---

## 17. Residual risks and assumptions

| # | Item | Status |
|---|---|---|
| 1 | **Doppler identifier correlation is unverified** (§11.2). No live API access in this phase. | Accepted with a hard floor: unprovable correlation → row stays `ambiguous`, canary aborts, never re-sends. Design is correct either way. Named (B) prerequisite. |
| 2 | `_request`'s terminal wrapper raises `AttributeError` on network errors (§10.1) — a pre-existing V1 defect. | Absorbed by classifying any post-dispatch exception as `ambiguous`. Not fixed here. Registered as debt. |
| 3 | Field-based attempt modelling overwrites history if a recipient is ever attempted twice (§3). | Cannot occur in this change (at most one attempt per row, by construction). `send_attempt_number` is the additive seam for a future `SendAttempt` table. |
| 4 | `select_for_update(skip_locked=True)` is not a guarantee on the SQLite dev/test backend (§4). | Correctness rests on the compare-and-set, not the lock. Race tests are PostgreSQL-only per the existing repo convention. |
| 5 | A transient 5xx or a timeout aborts the canary rather than retrying (§10.1). | Intentional. Refusing to guess is the whole point; the operator re-runs the canary from a clean `BulkSend` rather than re-attempting a row. |
| 6 | The `sent` immutability guard is service-layer plus a `save()` net, not a DB trigger (§2.5). | Accepted. Compare-and-set is atomic and backend-portable; a trigger would break the reversible-migration requirement. |
| 7 | An `in_flight` row blocks the command even if its worker is genuinely dead (§6 rule 3). | Intentional: the process cannot distinguish live from dead. It ages into `stale_sending` and becomes a human decision. |
| 8 | PR2 remains the larger half of the change. | Reduced by rejecting the `BackgroundJob` lease (§7), the payload-builder extraction (§10), and the `SendAttempt` table (§3). If review still exceeds budget, split the migration + state module from the worker + command. |
| 9 | Django's `LOGGING` additivity over `DEFAULT_LOGGING` (§12.1). | Not left as an assertion — PR1 must include a before/after test on `django` / `django.request` logger level, handlers, and propagate. |
