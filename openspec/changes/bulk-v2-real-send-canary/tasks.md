# Tasks: Bulk V2 Real-Send Canary

Source of truth: `openspec/changes/bulk-v2-real-send-canary/design.md`
(APPROVED, D1-D13). Every task below implements a specific design section
verbatim; do not deviate from field names, module paths, function names,
settings names, exit codes, or event names fixed there. Spec traceability
is in design.md §15 and is not repeated per-task except where a task maps
to more than one requirement.

## Binding scope statement (read before doing anything in this file)

(A) `sdd-apply` for this change may modify ONLY: code, tests, migrations,
settings, and documentation (`.env.example`, `ops/README.md`). Nothing else.

(B) Activating any of the six `BULK_PROCESSING_V2_REAL_SEND_*` flags,
selecting a real recipient, invoking `bulk_v2_real_send` against a real
`BulkSend`, and sending 1 real email are explicitly OUT OF SCOPE of every
task in this file. (B) requires separate, later, explicit owner
authorization outside this SDD change (proposal, "(A) Build vs (B)
execute — binding separation"; spec requirement "Completing This
Capability Is Never Authorization To Execute A Real Send").

No task in this file may execute a real Doppler call. Every test that
exercises a send path MUST mock the Doppler transport
(`DopplerRelayClient.session.request` or `_request`, per design §14
"Zero execution during apply"). Task PR1-T8 and PR2-T18 make this a
tested, not promised, property.

`sdd-apply` for this change does NOT run the management command, does NOT
run migrations against any real database beyond the test database used
by the test runner, and does NOT touch Doppler, production, or any
`.env` value beyond `.env.example` documentation.

---

## PR1 — Scaffolding, incapable of sending by construction

**Constraint (design §14): PR1 must contain zero code capable of reaching
Doppler.** Mechanical review criterion, enforced by PR1-T9 below: the PR1
diff must contain no NEW reference to `DopplerRelayClient`,
`send_template_message`, `doppler_relay`, `requests`, `BackgroundJob`,
`management/commands`, `BulkSendRecipient`, or `migrations`, except
purely documentary/test mentions that create no executable path — and
any such exception must be justified inline in the task, not silently
allowed. (No task below takes such an exception; if one becomes
necessary during implementation, `sdd-apply` must flag it rather than
proceed silently.)

Estimated PR1 total: **~230-260 lines** (settings ~20, gate module
~90-110, LOGGING dict ~25, tests ~220-260 — test volume is expected to
exceed implementation volume, which is normal for a pure-function gate
with 14 refusal codes).

### Settings

- [x] **PR1-T1** — Add the six `BULK_PROCESSING_V2_REAL_SEND_*` settings to
  `config/settings.py`, placed near the existing
  `BULK_PROCESSING_V2_CANARY_*` block (currently `settings.py:158-169`),
  using the exact `env(...)` idiom already used there. Verbatim per
  design §9:
  ```python
  BULK_PROCESSING_V2_REAL_SEND_ENABLED = env.bool("BULK_PROCESSING_V2_REAL_SEND_ENABLED", default=False)
  BULK_PROCESSING_V2_REAL_SEND_USER_IDS = env("BULK_PROCESSING_V2_REAL_SEND_USER_IDS", default="")
  BULK_PROCESSING_V2_REAL_SEND_REQUEST_IDS = env("BULK_PROCESSING_V2_REAL_SEND_REQUEST_IDS", default="")
  BULK_PROCESSING_V2_REAL_SEND_TEMPLATE_IDS = env("BULK_PROCESSING_V2_REAL_SEND_TEMPLATE_IDS", default="")
  BULK_PROCESSING_V2_REAL_SEND_RECIPIENT_DOMAINS = env("BULK_PROCESSING_V2_REAL_SEND_RECIPIENT_DOMAINS", default="")
  BULK_PROCESSING_V2_REAL_SEND_MAX_ROWS = env.int("BULK_PROCESSING_V2_REAL_SEND_MAX_ROWS", default=1)
  ```
  All defaults are inert (disabled / empty / `1`). No production value is
  set. ~10 lines. Traces: spec `bulk-v2-real-send-authorization` /
  "Six Independent Flags And A Single Kill Switch".

- [x] **PR1-T2** — Document the six flags in `.env.example`: name, one-line
  purpose, and their inert default, matching the existing style used for
  `BULK_PROCESSING_V2_CANARY_*` entries. Do not fill in any real value
  (no template id, no domain, no user id). ~10 lines.

  **Deviation, flagged explicitly**: documented in `DEPLOY.md` instead of
  `.env.example`. Two independent reasons: (1) `.env.example` on disk does
  NOT actually document the `BULK_PROCESSING_V2_CANARY_*` family either —
  verified via grep across the repo — that family is documented in
  `DEPLOY.md` (the "TD-02B: activacion canary de importacion V2" section),
  so `DEPLOY.md` is the real established precedent this task's own "matching
  the existing style" instruction points to. (2) Independently, this
  `sdd-apply` session's tool permission system unconditionally denies direct
  Read/Write/Edit access to any `.env*`-pattern path, including
  `.env.example` — confirmed by direct tool attempts, all denied with "File
  is in a directory that is denied by your permission settings." No `.env`
  file, real or example, was read or modified. The six flags are documented
  in `DEPLOY.md` immediately after the existing canary block, same format.

### `relay/services/bulk_v2_real_send.py` (new module)

- [x] **PR1-T3** — Create `relay/services/bulk_v2_real_send.py` with the
  `RealSendDecision` frozen dataclass and the `evaluate_real_send(...)`
  pure function, exact signature per design §9:
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
  Hard constraints on this module, each independently verifiable:
  - No `import` of any ORM model, `django.db`, `settings`, `requests`,
    `logging` I/O side effects, or filesystem access. The function's only
    external dependency is `normalize_allowlist` imported from
    `relay/services/bulk_v2_canary.py` (design §9 — the *only* permitted
    reuse; no other symbol is imported from that module).
  - Must NOT call `evaluate_canary`, in either direction, on any path.
  - Must NOT share a parameter name with `evaluate_canary`'s signature
    (`send_now`, `scheduled_at`, `import_only`, `engine_enabled`,
    `canary_enabled`, `allow_external_template_lookup` are all absent
    here by construction, per the parameter list above).
  ~60-80 lines including the ordered check sequence below.

- [x] **PR1-T4** — Implement the ordered validation sequence inside
  `evaluate_real_send`, first failure wins, exact refusal codes from
  design §9 (all prefixed `real_send_`):
  1. `real_send_disabled` — `real_send_enabled is False`.
  2. `real_send_config_invalid` — any of the four allowlist strings fails
     `normalize_allowlist` (its `"canary_config_invalid"` sentinel is
     translated to `real_send_config_invalid`; the canary string itself
     is never propagated outward), OR `max_rows != 1` exactly (not
     `<= 1` — reject `0`, `2`, any non-integer, per design §9's explicit
     "un-widenable by environment alone" rule mirroring
     `ops/td02c_settings_gate.py:67`).
  3. `real_send_user_allowlist_empty` — normalized user allowlist is empty.
  4. `real_send_request_allowlist_empty` — normalized request allowlist
     is empty.
  5. `real_send_template_allowlist_empty` — normalized template
     allowlist is empty.
  6. `real_send_domain_allowlist_empty` — normalized domain allowlist is
     empty.
  7. `real_send_request_id_required` — `client_request_id` falsy/empty.
  8. `real_send_request_not_allowed` — `client_request_id` not in the
     request allowlist.
  9. `real_send_user_not_allowed` — `user_id` not in the user allowlist.
  10. `real_send_template_not_allowed` — `template_id` not in the
      template allowlist (keyed on `template_id` ONLY — no
      `template_name` parameter exists on this function at all).
  11. `real_send_domain_not_allowed` — any domain in
      `recipient_domains` not in the domain allowlist.
  12. `real_send_row_count_empty` — `eligible_row_count == 0`.
  13. `real_send_row_limit_exceeded` — `eligible_row_count > max_rows`
      (with `max_rows` already validated `== 1` by step 2, this is
      effectively `> 1`).
  14. Success: `real_send_allowed`, `allowed=True`.
  ~40-60 lines (part of PR1-T3's line budget above).

### `LOGGING` configuration

- [x] **PR1-T5** — Add the `LOGGING` dict to `config/settings.py`, placed
  after the `DOPPLER_RELAY`/`DOPPLER_REPORTS` blocks, verbatim per design
  §12.1:
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
  No `root` key, no `django` logger key — additive over Django's
  `DEFAULT_LOGGING` by construction. ~20 lines. Traces: spec
  `bulk-v2-real-send-observability` / "INFO-Level relay Logs Must Reach
  journald Via A LOGGING Configuration".

### Tests

- [x] **PR1-T6** — `relay/tests/test_bulk_v2_real_send_gate.py`: one test
  per refusal code from PR1-T4 (14 tests: 13 refusal codes +
  `real_send_allowed`), each asserting `allowed`/`code` on the returned
  `RealSendDecision` for a minimal input that isolates exactly that
  check (all other dimensions pre-satisfied so only the targeted
  dimension fails). Include the `MAX_ROWS == 1` exactness sub-cases
  explicitly: `max_rows=0` → `real_send_config_invalid`, `max_rows=2` →
  `real_send_config_invalid`, `max_rows="1"` (non-int) →
  `real_send_config_invalid` per design's "not `<=1`" rule. Also include
  a structural-independence test: `evaluate_real_send`'s module does not
  import `evaluate_canary`, and a parameter-name-disjointness assertion
  against `evaluate_canary`'s signature (via `inspect.signature`).
  ~90-120 lines.

- [x] **PR1-T7** — Redaction/no-secrets test in
  `test_bulk_v2_real_send_gate.py` or a dedicated
  `test_bulk_v2_real_send_logging.py`: construct a
  `bulk_v2_real_send_decision`-shaped log line using a distinctive
  sentinel value for `settings.DOPPLER_RELAY["API_KEY"]`, capture it via
  `self.assertLogs` or a captured `StreamHandler`, and assert the
  sentinel, any raw token, and any full email address do not appear in
  the formatted output. This is infrastructure-only in PR1 (no send code
  exists yet); it establishes the pattern PR2-T20 extends to the attempt
  and result events. ~30-40 lines. Traces: spec
  `bulk-v2-real-send-observability` / "Forbidden Content Is Never
  Logged".

- [x] **PR1-T8** — LOGGING-additivity test in
  `relay/tests/test_logging_config.py`: capture
  `logging.getLogger("django").level`,
  `logging.getLogger("django").handlers`,
  `logging.getLogger("django").propagate`, and the same three attributes
  for `logging.getLogger("django.request")`, both BEFORE the `LOGGING`
  dict would apply (i.e., against Django's own `DEFAULT_LOGGING` values)
  and confirm they are byte-identical to their values with this
  project's settings loaded. Additionally assert
  `logging.getLogger("relay.x").getEffectiveLevel() == logging.INFO` and
  that a captured `logger.info(...)` call from a `relay.*` logger reaches
  the `relay_stderr` handler using only a pre-existing, non-send log
  statement (e.g. an existing `relay/api.py` or `relay/services/jobs.py`
  logger call) — no real-send code required to validate this, per design
  §12.1 and spec "The logging fix requires no send capability to
  verify". ~40-50 lines. Traces: design §17 residual risk 9.

- [x] **PR1-T9** — Mechanical PR1-boundary test/checklist item: a
  repository-level check (can be a small script invoked by CI, or a
  documented manual `git diff` grep step performed before merge) that
  asserts the PR1 diff contains no occurrence of `DopplerRelayClient`,
  `send_template_message`, `doppler_relay`, `requests`, `BackgroundJob`,
  `management/commands`, `BulkSendRecipient`, or `migrations` outside of
  this tasks.md / design.md themselves. If implemented as a test, it
  greps the diff of files touched by PR1 against that token list and
  fails on any match not explicitly whitelisted inline in this task (no
  whitelist entries are anticipated for PR1). ~15-25 lines or a
  documented manual step — implementer's choice, but the check must be
  explicit and checkable, not assumed. Traces: design §14 "mechanical
  review criterion".

**PR1 rollup: ~90-110 lines (gate module) + ~10 (settings) + ~20
(LOGGING) + ~10 (.env.example) ≈ 130-150 implementation lines, plus
~200-235 test lines. Total PR1 ≈ 330-385 lines.**

---

## PR2 — The V2 real-send engine

**First Doppler-capable path. `sdd-apply` still performs zero real
execution** (design §14 "Zero execution during apply" — the flag
defaults `False`, every allowlist defaults empty, so check 12 of §8.2
refuses unconditionally after apply completes).

Given design §17 residual risk 8 ("PR2 remains the larger half") and the
proposal's explicit instruction to split further now rather than defer
discovery to implementation, **PR2 is split into two reviewable
sub-batches**, landed as either two sequential PRs or two clearly
separated commits within PR2 at the implementer's/reviewer's discretion:

- **PR2a — schema + state module** (migration, model fields/constraints,
  `bulk_v2_send_state.py`, state-machine tests). Estimated ~280-320
  lines.
- **PR2b — worker + Doppler single-attempt + dispatcher + command +
  runbook** (`bulk_v2_send.py`, `doppler_relay.py` two-line change,
  `jobs.py` branch, management command, all crash/restart/concurrency
  and command tests, runbook doc). Estimated ~360-420 lines.

This split is stated explicitly here per the user's requirement; if
either sub-batch still threatens to exceed a reviewable budget during
`sdd-apply`, split further along the same seam (e.g. command tests into
their own follow-up commit) rather than deferring discovery.

### PR2a — Schema and state module

- [ ] **PR2a-T1** — Migration file
  `relay/migrations/20260808120000_bulk_v2_real_send_state.py`, with
  `dependencies = [("relay", "20260514150001_bulk_processing_v2_ledger")]`
  (confirmed current head migration on disk), per design §13. Operations,
  in this exact order, every one additive:
  1. `AddField` × 9 on `bulksendrecipient` — see PR2a-T2 for exact field
     definitions.
  2. `AddIndex` `bulk_recipient_send_status_idx` on
     `(bulk_send, send_status)`.
  3. `AddConstraint` × 6 — see PR2a-T3 for exact constraints.
  4. `AlterField` on `backgroundjob.job_type`, widening `choices` to
     include `("bulk_send_v2_real", "Bulk send V2 real")`.
  No `RunPython`, no `RunSQL`, no data migration — fully automatic
  reverse per Django's built-in inverses. Explicitly do NOT touch
  `status`, `bulk_recipient_valid_status`,
  `uniq_bulk_import_source_row`, `bulk_recipient_import_version_gte_1`,
  `bulk_recipient_source_row_gte_1`, `bulk_recipient_status_idx`, or
  `bulk_recipient_order_idx` (design §2.6). ~40-60 lines. Traces: spec
  `bulk-v2-send-state-machine` / "Migration Is Additive And Reversible".

- [ ] **PR2a-T2** — Add the nine new fields to `BulkSendRecipient` in
  `relay/models.py`, plus the class-level status constants, exact per
  design §2.1:
  ```python
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

  send_status = models.CharField(max_length=16, choices=SEND_STATUS_CHOICES, default=SEND_NOT_STARTED)
  send_attempt_number = models.PositiveIntegerField(default=0)
  send_started_at = models.DateTimeField(null=True, blank=True)
  sent_at = models.DateTimeField(null=True, blank=True)
  send_error_code = models.CharField(max_length=64, blank=True, default="")
  send_error_message = models.CharField(max_length=255, blank=True, default="")
  send_message_id = models.CharField(max_length=128, blank=True, default="")
  send_location = models.CharField(max_length=255, blank=True, default="")
  send_job_id = models.BigIntegerField(null=True, blank=True)
  ```
  `send_job_id` is a plain `BigIntegerField`, deliberately **not** a
  `ForeignKey` (design §2.1 rationale: `BackgroundJob` must have no
  referential authority over the ledger). No response-body/payload
  snapshot field — only classified `send_error_code` and
  `send_error_message = str(exc)[:255]` are ever persisted. ~35 lines.

- [ ] **PR2a-T3** — Add the six `CheckConstraint`s to
  `BulkSendRecipient.Meta.constraints`, verbatim per design §2.4:
  `bulk_recipient_valid_send_status`,
  `bulk_recipient_invalid_never_sends`,
  `bulk_recipient_send_started_at_consistent`,
  `bulk_recipient_send_attempt_number_consistent`,
  `bulk_recipient_sent_requires_sent_at`,
  `bulk_recipient_send_outcome_requires_error_code`. Use the exact
  `condition=models.Q(...)` expressions from design §2.4 (biconditional
  forms, not one-directional checks). Append after the four existing
  constraints without modifying them. ~45 lines.

- [ ] **PR2a-T4** — Add the `save()` terminal-state guard to
  `BulkSendRecipient`, mirroring the existing immutability convention at
  `relay/models.py:369-382`, extended per design §2.5 point 1: read
  `previous = type(self).objects.filter(pk=self.pk).values(...)`
  including `send_status`, and raise `ValueError` when
  `previous["send_status"] == "sent" and self.send_status != "sent"`,
  and when `previous["send_status"] in SEND_TERMINAL and
  self.send_status != previous["send_status"]`. Documented as a safety
  net (extra query per save, not atomic), not the primary transition
  mechanism — the primary mechanism is the compare-and-set `UPDATE` in
  `bulk_v2_send_state.py` (PR2a-T5). ~15-20 lines.

- [ ] **PR2a-T5** — New module `relay/services/bulk_v2_send_state.py`
  containing, exact names per design §3/§4/§5/§6/§14:
  - `SendStateTransitionError` (exception class).
  - `STALE_SENDING_AFTER = timedelta(seconds=10 * settings.DOPPLER_RELAY["TIMEOUT"])`
    — a **module constant**, not a 7th Django setting (design §5 — it
    authorizes nothing, so it must not blur the six-flag authorization
    surface). Confirm `DOPPLER_RELAY["TIMEOUT"] == 30` at
    `config/settings.py:140` so this evaluates to 300s.
  - The legal-edge whitelist module constant:
    `{("not_started","sending"), ("sending","sent"),
    ("sending","send_failed"), ("sending","ambiguous")}`.
  - `claim_next_recipient(bulk_send_id: int, *, job_id: int) -> int | None`
    — exact `select_for_update(skip_locked=True)` inside
    `transaction.atomic()` + compare-and-set `UPDATE ... WHERE
    send_status='not_started'` pattern from design §4's code block,
    including the `status=STATUS_PENDING` filter (invariant 1, layer 1),
    `order_by("import_version", "source_row_number")` for deterministic
    claim order, `F("send_attempt_number") + 1`, and explicit
    `updated_at=now` (because `.update()` bypasses `auto_now`).
  - `mark_sent(row_pk, *, message_id, location, now=None)`,
    `mark_send_failed(row_pk, *, error_code, error_message, now=None)`,
    `mark_ambiguous(row_pk, *, error_code, error_message, now=None)` —
    each a compare-and-set `UPDATE ... WHERE pk=row_pk AND
    send_status='sending'` (only legal `FROM` state for all three),
    raising `SendStateTransitionError` if `updated != 1`.
  - `describe_send_ledger(bulk_send_id: int, *, now=None) -> SendLedger`
    — the `LedgerRow`/`SendLedger` frozen dataclasses exact per design
    §6's field lists, and the single-statement, no-join, no-file-access
    query from §6, and the 7-way mutually-exclusive classification table
    (`excluded`, `eligible`, `in_flight`, `stale_sending`,
    `terminal_sent`, `terminal_failed`, `blocked_ambiguous`) per design
    §6.
  - The ordered 5-rule recovery decision documented as a docstring/helper
    consumed by the management command (PR2b-T4): (1) `blocked_ambiguous`
    non-empty → abort; (2) `stale_sending` non-empty → abort; (3)
    `in_flight` non-empty → abort; (4) `eligible` empty → clean no-op;
    (5) otherwise → proceed to the gate.
  - Runtime guard, called at the top of `process_bulk_id_v2` (PR2b-T1):
    `if not transaction.get_autocommit(): raise SendStateError(...)` per
    design §4, converting "committed before the call" from a review
    promise into a tested runtime invariant.

  Zero `csv` import, zero reference to `recipients_file` or
  `BulkImportService` anywhere in this module (design §6 "Zero CSV
  dependency is structural"). Every `send_status=` write in the entire
  codebase occurs only in this module (grep-provable, verified by
  PR2a-T9). ~130-160 lines.

- [ ] **PR2a-T6** — State-machine unit tests,
  `relay/tests/test_bulk_v2_send_state.py`: one test per invariant in
  design §2.3's table (6 invariants), each attempting the forbidden
  state/transition directly and asserting rejection (constraint
  `IntegrityError` for DB-enforced invariants, `SendStateTransitionError`
  or `ValueError` for service-layer-enforced ones):
  1. `status="invalid"` row cannot reach `send_status="sending"` or
     `"sent"` via direct `.save()` (constraint) nor via
     `claim_next_recipient` (service-layer filter) — both paths tested
     separately per spec's "regardless of which layer enforces the
     rejection".
  2. `sent` requires `sent_at` non-null (attempt to persist `sent` with
     `sent_at=None` raises `IntegrityError`).
  3. `sending` requires `send_started_at` non-null (same pattern).
  4. `ambiguous` unreachable directly from `not_started` — attempt via
     `mark_ambiguous` on a `not_started` row raises
     `SendStateTransitionError`; only reachable via `sending`.
  5. `send_failed` requires non-empty `send_error_code` (constraint
     violation on empty string).
  6. No `sent -> sending` transition: after `mark_sent`, any subsequent
     `.save()` setting `send_status` back to `sending` raises
     `ValueError` (the `save()` guard from PR2a-T4); additionally no
     compare-and-set path in `bulk_v2_send_state.py` targets `sending`
     as a `SET` value with `sent` as the expected `WHERE`, verified by
     inspecting the legal-edge whitelist.
  Also: a full-graph enumeration test asserting every transition
  produced across this test file's full run is one of exactly
  `{not_started->sending, sending->sent, sending->send_failed,
  sending->ambiguous}` (spec "Only the defined forward edges are
  reachable"). ~110-140 lines. Traces: spec `bulk-v2-send-state-machine`
  / all six companion-data and transition-graph requirements.

- [ ] **PR2a-T7** — `claim_next_recipient` unit tests: successful claim
  transitions `not_started -> sending` with `send_started_at` set and
  `send_attempt_number` incremented to 1, committed (verifiable by a
  fresh query in the same test); claim returns `None` when no eligible
  row exists; claim skips `status="invalid"` rows even when
  `send_status="not_started"` (spec "Claiming logic excludes invalid
  rows" scenario, tested directly — this is the 8th crash/concurrency
  proof surface but stated here as the ordinary-path claim contract, not
  one of the 8 numbered scenarios below); a simulated stale-claim test
  (mutate the row's `send_status` between read and update to prove the
  compare-and-set rejects on a lost race, backend-independent per design
  §17 risk 4). ~50-70 lines.

- [ ] **PR2a-T8** — `describe_send_ledger` unit tests: one test per
  classification bucket (7 buckets) using rows constructed to land in
  exactly one bucket each, asserting mutual exclusivity across a
  multi-row `BulkSend` fixture containing one row per bucket
  simultaneously. Include an aged-`sending` fixture
  (`send_started_at = now - STALE_SENDING_AFTER - 1s`) landing in
  `stale_sending`, and a fresh-`sending` fixture
  (`send_started_at = now - 1s`) landing in `in_flight`, proving the
  300s boundary is applied correctly. ~40-60 lines.

- [ ] **PR2a-T9** — Grep-provable structural test: assert that
  `send_status=` (as an assignment target, i.e. `send_status=...` in an
  `.update(...)` call or `self.send_status = ...`) appears only in
  `relay/services/bulk_v2_send_state.py` and the migration file
  `20260808120000_bulk_v2_real_send_state.py` across the entire `relay/`
  tree (implemented as a Python test that walks the source tree and
  greps, or as a documented CI grep step). This is the mechanism proving
  design §2.5's "every send_status write goes through exactly one
  module" claim. ~20-30 lines.

**PR2a rollup: migration ~50 + model fields ~35 + constraints ~45 +
save() guard ~20 + state module ~145 ≈ 295 implementation lines, plus
~250-300 test lines. Total PR2a ≈ 545-595 lines** — note this alone is
already close to a full reviewable budget; if it proves too large in
practice, PR2a-T1..T4 (schema) may be landed as its own sub-commit ahead
of PR2a-T5..T9 (state module), per design §17 risk 8's suggested further
split.

### PR2b — Worker, Doppler single-attempt, dispatcher, command, runbook

- [ ] **PR2b-T1** — New module `relay/services/bulk_v2_send.py`
  containing `process_bulk_id_v2` and `build_single_attempt_client`,
  exact per design §10/§14. `process_bulk_id_v2` must NOT be added to
  `relay/services/bulk_processing.py` — that file must remain a
  **zero-line diff** for this entire change (design §14's explicit
  refinement of the proposal; the legacy guard at
  `bulk_processing.py:39-42` stays untouched by inspection). Structure:
  1. Entry-point runtime guard: `if not transaction.get_autocommit():
     raise` (calls into `bulk_v2_send_state`'s guard, PR2a-T5).
  2. Loop: `claim_next_recipient(bulk_send_id, job_id=job_id)`; if
     `None`, stop (nothing left to claim, matching the ledger's
     `eligible` classification).
  3. Emit `bulk_v2_real_send_attempt` (PR2b-T5) immediately after the
     `sending` transition is confirmed committed, before the outbound
     call.
  4. `build_single_attempt_client() -> DopplerRelayClient` returning
     `DopplerRelayClient(max_attempts=1)` (depends on PR2b-T2).
  5. Assert `client.max_attempts == 1` immediately before calling
     `send_template_message` (design §10's defensive check), raising
     `SendStateError` otherwise.
  6. Call the existing, unmodified `send_template_message` — no
     duplicated payload builder.
  7. Apply the outcome classifier table (PR2b-T3) to whatever the call
     returns or raises, calling `mark_sent`/`mark_send_failed`/
     `mark_ambiguous` accordingly, in a transaction separate from any
     that was open during the call.
  8. Emit `bulk_v2_real_send_result` (PR2b-T5) after the terminal
     transition commits.
  ~70-90 lines.

- [ ] **PR2b-T2** — Two-line change to
  `relay/services/doppler_relay.py`, exact per design §10:
  1. `DopplerRelayClient.__init__` gains keyword `max_attempts: int = 3`,
     stored as `self.max_attempts = max(int(max_attempts), 1)`.
  2. `_request()` line 227, currently `max_retries = 3`, becomes
     `max_retries = self.max_attempts`. Loop body at lines 231-293 is
     byte-identical otherwise.
  Confirm no other line in `doppler_relay.py` changes. Every existing V1
  construction site instantiates `DopplerRelayClient()` with no
  `max_attempts` argument, so `self.max_attempts == 3` there and V1's
  retry bound, backoff (`min(0.8 * 2**retry_count, 8)`), and logging are
  unchanged. ~2 lines changed.

- [ ] **PR2b-T3** — Implement the outcome classifier inside
  `bulk_v2_send.py` (or a small helper it calls), exact mapping per
  design §10.1's table:
  - 2xx + parsed body → `sent` (even with empty `send_message_id` — the
    2xx status alone proves acceptance, per design §11.5; the
    correlatable-identifier requirement is a canary PASS-criterion
    concern, not a state-machine concern).
  - `DopplerRelayError` with `status == 402` and Doppler
    `errorCode == 1` → `send_failed`, `error_code="doppler_quota_exceeded"`.
  - `DopplerRelayError` with `status` in 400-499 except 408/429 →
    `send_failed`, `error_code=f"doppler_http_{status}"`.
  - `DopplerRelayError` with `status` in {408, 429} or `>= 500` →
    `ambiguous`, `error_code=f"doppler_http_{status}"`.
  - `DopplerRelayError` with `status` absent/`None` → `ambiguous`,
    `error_code="doppler_error_no_status"`.
  - `ValueError`/`JSONDecodeError` from response parsing → `ambiguous`,
    `error_code="response_unparseable"`.
  - `requests.Timeout` / `ConnectionError` / any `RequestException` →
    `ambiguous`, `error_code` one of `"timeout"` / `"connection_error"`
    / `"request_exception"`.
  - **Any other exception, explicitly including `AttributeError`** →
    `ambiguous`, `error_code="dispatch_exception"` (see PR2b-T9 for the
    dedicated test).
  - Exception raised before the socket write (payload validation, e.g.
    `ValueError` at `doppler_relay.py:599`) → `send_failed`,
    `error_code="payload_invalid"`.
  The classifier's except-clause structure must catch bare `Exception`
  as the final fallback (not just `DopplerRelayError`/`RequestException`)
  — this is load-bearing, see PR2b-T9. `send_error_message = str(exc)[:255]`;
  `exc.payload` (which contains `request_headers`/`Authorization`) is
  never read, logged, or persisted. ~30-40 lines.

- [ ] **PR2b-T4** — One additive `elif` branch in `dispatch_background_job`
  (`relay/services/jobs.py`, after the existing check at line 48):
  ```python
  if job.job_type == BackgroundJob.TYPE_BULK_SEND_V2_REAL:
      from relay.services.bulk_v2_send import process_bulk_id_v2
      return process_bulk_id_v2(job.bulk_send_id, job_id=job.id)
  ```
  Import is function-local, per design §14, so the existing module-level
  `process_bulk_id` import is untouched. Add
  `TYPE_BULK_SEND_V2_REAL = "bulk_send_v2_real"` as a `BackgroundJob`
  class constant alongside the existing `TYPE_BULK_SEND`/`TYPE_POST_REPORT`
  (this is the choice widened by PR2a-T1's `AlterField`). ~6-10 lines.

- [ ] **PR2b-T5** — Implement the three log events inside
  `bulk_v2_send.py` (attempt/result) and `bulk_v2_real_send.py`
  (decision — note: `evaluate_real_send` itself stays pure per PR1-T3;
  the decision event is emitted by its **caller**, the management
  command, not by the gate function itself — confirm this placement
  explicitly since design §9 requires the gate module to have zero I/O).
  Exact field sets and flat `key=value` format per design §12.2:
  - `bulk_v2_real_send_decision`: `decision` (`allowed`|`refused`),
    `code`, `bulk_send_id`, `request=<sha256(client_request_id)[:12]>`
    (reusing the existing convention at `relay/api.py:450-455`),
    `eligible_rows`, `max_rows`, `at`.
  - `bulk_v2_real_send_attempt`: `bulk_send_id`,
    `recipient_key=<idempotency_key>`, `recipient_domain`, `job_id`,
    `attempt_number`, `send_started_at`.
  - `bulk_v2_real_send_result`: `bulk_send_id`, `recipient_key`,
    `recipient_domain`, `job_id`, `attempt_number`, `result`
    (`sent`|`send_failed`|`ambiguous`), `error_code`, `message_id`,
    `location`, `send_started_at`, `finished_at`, `duration_ms`.
  All three emitted via `logging.getLogger(__name__)` at `INFO` (the
  `relay.*` logger tree, covered by PR1-T5's `LOGGING` dict). ~30-40
  lines.

### Management command

- [ ] **PR2b-T6** — New file
  `relay/management/commands/bulk_v2_real_send.py`. Argument surface,
  exact per design §8.1: `--bulk-send-id` (`type=int`, `required=True`,
  no default, no `--latest`, no `--all`, no positional fallback) and
  `--dry-run` (`action="store_true"`). Nothing else — no `--force`, no
  `--retry-ambiguous`, no `--yes`, no recipient argument (wildcard or
  literal), no `--template`, no `--file`, no `--limit`. ~15-20 lines for
  `add_arguments`.

- [ ] **PR2b-T7** — Implement the 14 ordered checks in the command's
  `handle()`, first failure wins, exact codes/exits per design §8.2's
  table:
  | # | Check | Code | Exit |
  |---|---|---|---|
  | 1 | `--bulk-send-id` present and integral | argparse error | 2 |
  | 2 | `BULK_PROCESSING_V2_REAL_SEND_ENABLED is True` | `real_send_disabled` | 5 |
  | 3 | `BulkSend` with that pk exists | `real_send_bulk_not_found` | 2 |
  | 4 | `engine_version == ENGINE_V2` | `real_send_engine_not_v2` | 2 |
  | 5 | `import_status in {ready, ready_with_errors}` | `real_send_import_not_ready` | 2 |
  | 6 | no queued/running `bulk_send_v2_real` job for this bulk | `real_send_job_already_present` | 4 |
  | 7 | ledger read (`describe_send_ledger`) | — | — |
  | 8 | `blocked_ambiguous` empty | `real_send_ambiguous_present` | 3 |
  | 9 | `stale_sending` empty | `real_send_stale_sending_present` | 4 |
  | 10 | `in_flight` empty | `real_send_in_flight_present` | 4 |
  | 11 | `eligible` non-empty | `real_send_nothing_to_send` | 0 |
  | 12 | `evaluate_real_send(...).allowed` | the decision's own code | 5 |
  | 13 | `--dry-run` | `real_send_dry_run` | 0 |
  | 14 | execute | `real_send_allowed` | 0 |

  Check 2 is a **cheap pre-DB kill-switch check, zero DB queries**,
  deliberately duplicating step 12's full `evaluate_real_send`
  evaluation (design §8.2 "the kill switch is check 2, before any
  database access"). Structural checks (3-5) precede the ledger (7-11);
  the ledger precedes the gate (12) because `evaluate_real_send` is pure
  and takes `eligible_row_count`/`recipient_domains` as inputs that only
  exist after the ledger read. Every check 2-13 emits exactly one
  `bulk_v2_real_send_decision` event and terminates via
  `CommandError(message, returncode=N)`. Execution (check 14) creates
  the `BackgroundJob` row, claims it via the **locked** claim path
  (`select_for_update(skip_locked=True).filter(pk=..., state=STATE_QUEUED)`
  mirroring `process_background_jobs.py:48-58`), then calls
  `run_claimed_job(job)` — never `run_background_job(job_id)` (design
  §7's bypass gap). ~90-120 lines.

- [ ] **PR2b-T8** — Ambiguous-handling behavior (design §8.3), as its own
  checkable sub-item of PR2b-T7: on finding a `blocked_ambiguous` row,
  the command reports it (`idempotency_key`, `send_started_at`,
  `send_attempt_number`, `send_error_code`, `send_message_id`,
  `send_location`, `send_job_id`) to stdout and to the
  `bulk_v2_real_send_decision` event, does NOT touch it (no re-send, no
  retry, no state change, no field clearing), refuses to process
  anything else in the same invocation, and exits 3. ~15-20 lines (part
  of PR2b-T7's check-8 branch).

### Management command verification tasks (each individually checkable)

- [ ] **PR2b-T9** — Test: command requires explicit `--bulk-send-id`; no
  `--latest`/`--all`/positional fallback exists; invoking without it
  fails via argparse before any DB read (spec "Command invoked without
  bulk_send_id fails before touching any row").
- [ ] **PR2b-T10** — Test: command accepts only `engine_version == v2`
  bulks; a `legacy`-engine `BulkSend` is refused at check 4 before any
  gate evaluation or Doppler call.
- [ ] **PR2b-T11** — Test: real-send gate checked twice — (a) cheap
  kill-switch check at check 2 with the flag `False`, asserting **zero
  DB queries** executed (via `django.test.utils.CaptureQueriesContext`
  or `assertNumQueries(0)`); (b) the full `evaluate_real_send` at check
  12 with the flag `True` but an allowlist mismatch, asserting DB reads
  did occur but the send was still refused.
- [ ] **PR2b-T12** — Test: exact-match requirement on user, request_id,
  template_id, and domain allowlists — four separate test cases, one per
  dimension, each with every other dimension matching and exactly one
  mismatching, asserting refusal names the correct dimension's code.
- [ ] **PR2b-T13** — Test: command processes at most 1 recipient;
  `MAX_ROWS` validated `== 1` (not `<=1`) via the gate call, and a
  fixture with 2 eligible rows is refused with `real_send_row_limit_exceeded`.
- [ ] **PR2b-T14** — Test: command's `parser._actions` does NOT include a
  `--force` dest.
- [ ] **PR2b-T15** — Test: command's `parser._actions` does NOT include a
  `--retry-ambiguous` dest.
- [ ] **PR2b-T16** — Test: command's `parser._actions` does NOT include
  any recipient wildcard or literal recipient dest (no `--recipient`,
  no `--email`, no positional recipient argument).
- [ ] **PR2b-T17** — Test: command never parses/reads the CSV — run the
  command (mocked Doppler) against a `BulkSend` whose `recipients_file`
  is deleted from disk before invocation, and assert identical behavior
  (exit code and DB end-state) versus a run with the file present. Also
  assert `csv` is not imported by the command module and
  `recipients_file`/`BulkImportService` are not referenced (grep-provable,
  matching design §6/§8.1).
- [ ] **PR2b-T18** — Test: command aborts (exit 3) if it finds an
  `ambiguous` row, touching nothing — assert the row's fields are
  byte-identical before and after the command run.
- [ ] **PR2b-T19** — Test: command aborts (exit 4) if it finds a stale
  `sending` row (`send_started_at` older than `STALE_SENDING_AFTER`),
  touching nothing — assert the row's fields are byte-identical before
  and after.
- [ ] **PR2b-T20** — Test: command never issues more than one Doppler
  call per invocation — assert the mocked transport's call count is at
  most 1 across every branch of the 14-check sequence, including the
  success path.
- [ ] **PR2b-T21** — Test: exact closed argument surface per design §8.1
  — assert `{a.dest for a in parser._actions}` equals
  `{"help", "bulk_send_id", "dry_run"}` plus Django `BaseCommand`'s
  standard defaults (`version`, `verbosity`, `settings`, `pythonpath`,
  `traceback`, `no_color`, `force_color`, `skip_checks` — enumerate the
  actual current Django version's defaults rather than assuming a fixed
  list, since this must be exact).

  (PR2b-T9 through PR2b-T21 collectively live in
  `relay/tests/test_bulk_v2_real_send_command.py`, ~200-260 lines total.)

### The 8 crash/restart/concurrency scenarios — each its own task/test

Every test below must explicitly assert, in its docstring/comment and
its assertions, which of the four hard requirements it proves: (a)
`sent` never re-sends; (b) `ambiguous` never auto-retries; (c) an aged
`sending` row never automatically returns to `not_started`; (d) no retry
path depends on the original CSV (proven by deleting/omitting the CSV
during the scenario and confirming behavior is unaffected). Not every
scenario proves all four — each task states exactly which apply.

- [ ] **PR2b-T22 — Scenario 1: Worker dies before the outbound Doppler
  call.** Commit a row's transition to `not_started -> sending` via
  `claim_next_recipient` (simulating the durable pre-call state), then
  simulate crash by never calling the mocked Doppler transport at all in
  this test. Assert on re-read: `send_status == "sending"`,
  `send_started_at` populated, zero Doppler calls made. Proves (a) is
  vacuously true (no send occurred) and establishes the baseline for (c)
  — the row is never auto-reset to `not_started` by anything in this
  test's assertions (no code runs that could reset it). File the CSV as
  deleted before the assertion to additionally establish (d) for this
  scenario. `relay/tests/test_bulk_v2_crash_scenarios.py`. ~20-30 lines.

- [ ] **PR2b-T23 — Scenario 2: Worker dies during the outbound call.**
  Mock the Doppler transport to raise mid-call (e.g. a mocked
  `requests.Timeout`) after the `sending` transition is already
  committed. Assert the classifier maps this to `ambiguous` via
  `mark_ambiguous`, `send_error_code` populated, `send_status` is a
  terminal state (not left at `sending`). Proves (b): assert no code
  path in the module subsequently transitions this row back out of
  `ambiguous` (grep-provable per PR2b-T29, cross-referenced here).
  Delete the CSV before running to establish (d). ~25-35 lines.

- [ ] **PR2b-T24 — Scenario 3: Doppler may have accepted the message but
  the process dies before persisting `sent`.** The scenario this whole
  design exists for. Mock the transport to return a successful response,
  but simulate the crash by not calling `mark_sent` at all (i.e., assert
  on the row as left in `sending` with no persisted `sent` state) —
  since no code can execute "persist" and then "crash" in a synchronous
  test, model it as: the row remains `sending` after a Doppler call
  succeeded, and assert that on a subsequent `describe_send_ledger` read
  the row is classified as `in_flight` or `stale_sending`, **never**
  `terminal_sent`, and that no code path infers `sent` from the mere
  fact a call was attempted. Proves (a) and (c): the row is never
  silently reported as `sent` on assumption, only on persisted evidence.
  ~25-35 lines.

- [ ] **PR2b-T25 — Scenario 4: Crash after persisting `sent` (clean
  no-op on recovery, exit 0).** Persist `send_status == "sent"` with
  `sent_at` set via `mark_sent`, then re-run the management command
  (mocked transport asserting zero calls) against the same
  `bulk_send_id`. Assert exit code 0, `real_send_nothing_to_send` (or
  equivalent all-terminal ledger state), and zero Doppler calls made on
  the second run. Proves (a): `sent` never re-sends. ~20-25 lines.

- [ ] **PR2b-T26 — Scenario 5: Service restart with a recipient row left
  in `sending`.** Persist a `sending` row (as in Scenario 1), then
  simulate "restart" by constructing a fresh `describe_send_ledger` call
  in a new test-level "process" boundary (no in-memory state carried
  over — use only DB state). Assert the row remains
  `send_status == "sending"` with its original `send_started_at`
  unchanged, and that no startup/scheduled code path in the codebase
  (verified: no signal handler, no `AppConfig.ready()` hook, no
  management command runs automatically) touches it. Proves (c)
  explicitly. ~20-25 lines.

- [ ] **PR2b-T27 — Scenario 6: Two concurrent workers race for the same
  row (PostgreSQL-only).** New file
  `relay/tests/test_bulk_v2_real_send_postgresql.py`, decorated
  `@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")`
  on a `TransactionTestCase`, mirroring `test_bulk_v2_postgresql.py:17-27`
  exactly (real threads, not mocked concurrency). Two threads both call
  `claim_next_recipient` against the same eligible row simultaneously;
  assert exactly one thread receives the row's pk and the other receives
  `None`; assert exactly one Doppler call would be made (mocked
  transport call count == 1 after both threads complete their full
  claim-to-terminal cycle). Proves the single-writer claiming
  requirement generally (not conditioned on `MAX_ROWS == 1`, per spec).
  ~40-60 lines.

- [ ] **PR2b-T28 — Scenario 7: The same `BackgroundJob` executed twice
  (via `run_background_job` bypass path).** Create a `BackgroundJob` of
  type `bulk_send_v2_real`, call `run_background_job(job.id)` twice in
  sequence (the exact bypass path identified at `jobs.py:68-69` that
  skips `claim_next_job()`'s lock). Assert at most one Doppler call
  occurred across both invocations for the single eligible row (the
  second invocation's `claim_next_recipient` call returns `None` because
  the row is already past `not_started`). Proves (a) and demonstrates
  the per-recipient compare-and-set is the actual safety boundary, not
  job-level locking (design §7). ~30-40 lines.

- [ ] **PR2b-T29 — Scenario 8: The management command executed
  repeatedly against the same `bulk_send_id` (idempotent no-op on the
  second run, exit 0 per `real_send_nothing_to_send`).** Run the full
  command successfully once (mocked transport, one eligible row reaches
  `sent`), then run it again against the same `--bulk-send-id`. Assert
  the second run exits 0, hits check 11's `real_send_nothing_to_send`
  branch, and makes zero Doppler calls. This overlaps with PR2b-T25 but
  exercises the full command path (all 14 checks) rather than just the
  ledger/state layer directly — keep both, since PR2b-T25 proves the
  state-machine property and this proves the operator-facing command
  contract. ~20-25 lines.

**Traces for PR2b-T22 through PR2b-T29 collectively**: spec
`bulk-v2-send-state-machine` / all eight named crash/restart scenario
blocks; proposal success criteria "sent is never re-sent; job retry
touches no terminal row; two workers never share a row; stale sending
never auto-returns to not_started; ambiguous never auto-retries."

### V1 pre-existing defect — register as debt, absorb, do NOT fix

- [ ] **PR2b-T30** — Add a code comment / docstring note (NOT a fix) in
  `relay/services/doppler_relay.py` near `_request`'s terminal wrapper
  (currently lines 286-293), documenting the discovered defect exactly:
  `getattr(last_error, 'response', {}).status_code` fails with
  `AttributeError` (not `DopplerRelayError`) on `requests.Timeout`/
  `ConnectionError`, because `last_error.response` **exists and is
  `None`** so `getattr`'s default never applies, and `None.status_code`
  raises. State explicitly in the comment: "Pre-existing V1 defect,
  discovered during bulk-v2-real-send-canary design (design.md §10.1).
  Not fixed here — V1 must stay byte-identical. V2's send path
  (`bulk_v2_send.py`) absorbs this by classifying any post-dispatch
  exception, including `AttributeError`, as `ambiguous`." Do not change
  any executable line of `doppler_relay.py` beyond PR2b-T2's two lines.
  Also record this as a tracked follow-up item in `ops/README.md` or a
  project issue tracker reference (whichever this repo's convention
  uses) — a note, not a code change. ~5-10 lines (comment only).

- [ ] **PR2b-T31 — Dedicated test proving fail-closed absorption of the
  V1 defect.** In `relay/tests/test_bulk_v2_crash_scenarios.py` (or
  `test_doppler_single_attempt.py`), mock the Doppler transport/client
  call site to raise a bare `AttributeError` directly (simulating
  exactly what `_request` would raise on a real `requests.Timeout` given
  the V1 defect — do not attempt to reproduce the defect by calling real
  `_request` machinery; construct the `AttributeError` directly at the
  mock boundary to isolate the V2 classifier's behavior from V1's
  internals). Assert: (1) `process_bulk_id_v2` does not propagate the
  `AttributeError` uncaught; (2) the row's terminal state is
  `send_status == "ambiguous"`, `send_error_code == "dispatch_exception"`;
  (3) the terminal transition **commits** — re-read the row from the DB
  in the same test and confirm it is not left at `send_status ==
  "sending"`; (4) no row is silently stuck. This is the "any other
  exception" branch of PR2b-T3's classifier table. ~25-35 lines. Traces:
  design §10.1, §17 residual risk 2; user's explicit requirement that
  "the terminal transition to ambiguous still commits even when the
  exception type is unexpected."

### Doppler correlation constants — explicit, no false assumptions

- [ ] **PR2b-T32** — Add the two correlation constants as documented
  module-level comments in `relay/services/bulk_v2_send_state.py` (near
  `send_message_id`/`send_location` field usage) or as a docstring on
  `mark_sent`, verbatim:
  ```
  # DOPPLER_SERVER_SIDE_IDEMPOTENCY = NOT_DOCUMENTED
  # DOPPLER_AMBIGUOUS_RECONCILIATION = NOT_PROVEN
  #
  # Neither send_message_id nor the Location header's trailing segment
  # is confirmed to enable a safe retry. No code in this change queries
  # Doppler to resolve an ambiguous row, and no automatic or manual
  # ambiguous -> sent or ambiguous -> retry transition exists anywhere
  # in this module or in bulk_v2_send.py. See design.md §11.
  ```
  ~8-12 lines (comment only).

- [ ] **PR2b-T33 — Structural test asserting zero `ambiguous -> sent` /
  `ambiguous -> retry` code path exists anywhere.** Implemented as a
  grep-provable test: walk `relay/services/bulk_v2_send_state.py` and
  `relay/services/bulk_v2_send.py` source, and assert no function
  performs an `UPDATE`/`.save()` with `send_status="ambiguous"` in its
  `WHERE`/expected-from clause paired with a target `send_status` other
  than `"ambiguous"` itself — i.e., assert the legal-edge whitelist
  (PR2a-T5) contains no tuple whose first element is `"ambiguous"`.
  Assert also that no function name or code path in either module is
  named or behaves like a reconciliation/retry entry point (no
  `resolve_ambiguous`, `retry_ambiguous`, `reconcile_ambiguous`, or
  similar symbol exists in the codebase — grep for these token
  fragments returns zero matches outside test/design/tasks files).
  ~20-30 lines. Traces: spec `bulk-v2-send-state-machine` / "Ambiguous
  Outcomes Require Human Resolution With Correlatable Evidence";
  user's explicit non-negotiable-floor requirement.

### `doppler_relay.py` behavior-preservation tests

- [ ] **PR2b-T34** — `relay/tests/test_doppler_single_attempt.py`: (a)
  assert `DopplerRelayClient()`'s default `max_attempts == 3`; (b) a
  mocked transport that always raises is invoked exactly 3 times for a
  default-constructed client and exactly 1 time for a
  `DopplerRelayClient(max_attempts=1)` client; (c) confirm
  `build_single_attempt_client()` returns a client with `max_attempts ==
  1`; (d) run the **existing, untouched V1 test suite** and confirm it
  passes with zero modifications, explicitly confirming V1's default
  behavior of 3 attempts is unchanged by the two-line change in
  PR2b-T2. ~40-50 lines (plus running, not modifying, the existing V1
  suite).

### Test-infrastructure — no real HTTP call anywhere

- [ ] **PR2b-T35** — Suite-wide assertion/fixture proving no test in
  this change's added test files constructs a `DopplerRelayClient`
  against a non-mocked transport. Implementable as: (a) a shared test
  fixture/base class used by every new PR2 test file that patches
  `requests.Session.request` (or `DopplerRelayClient`'s underlying
  transport call) to raise `RuntimeError("real HTTP call attempted in
  test")` by default, so any test that forgets to mock fails loudly
  rather than silently reaching the network; and (b) a meta-test that
  greps all new test files for `DopplerRelayClient(` construction sites
  and confirms each is paired with a mock/patch of the transport in the
  same test or its `setUp`. ~25-35 lines. Traces: design §14 "No test
  performs a real HTTP call"; user's explicit requirement 6.

### Runbook

- [ ] **PR2b-T36** — Add the real-send runbook section to
  `ops/README.md`, covering activation (which flags, which allowlist
  values, in what order), the single execution (`manage.py
  bulk_v2_real_send --bulk-send-id N`, then without `--dry-run`),
  evidence capture (the (B) prerequisite from design §11.4: capture one
  real accepted response's status/`Location`/JSON body before relying on
  correlation; the PASS criteria from design §11.5 requiring a non-empty
  `send_message_id`), and deactivation (clear
  `BULK_PROCESSING_V2_REAL_SEND_ENABLED` and all four allowlists,
  restart `django.service`). Include the one-line ORM query for manually
  inspecting an aged `sending` row (design §5) and for resolving an
  `ambiguous` row by hand (design §11.3 — a human writing to the
  database under this runbook, not a feature). **Written only — this
  task does not execute any part of the runbook.** ~60-90 lines
  (documentation, not counted against the code line budget).

**PR2b rollup: worker+classifier ~110 + doppler_relay.py 2 lines +
jobs.py ~8 + command ~110 + comments ~20 ≈ 250 implementation lines,
plus command-verification tests ~230 + 8 scenario tests ~230 +
V1-defect-absorption test ~35 + correlation-constants test ~30 +
single-attempt tests ~45 + no-real-HTTP fixture ~30 ≈ 600 test lines.
Total PR2b ≈ 850-900 lines** — this exceeds a single reviewable PR
budget on its own. **Explicit further split recommended**: land
PR2b-T1..T8 (worker, Doppler change, dispatcher, command implementation)
as one commit/PR (~250 impl + ~230 command-verification tests ≈ 480
lines), and PR2b-T22..T35 (the 8 scenario tests + defect-absorption +
correlation + single-attempt + no-real-HTTP fixture, ~600 lines,
zero new implementation) as a second commit/PR reviewed primarily as
test coverage over already-reviewed code. PR2b-T30/T32 (comments) and
PR2b-T36 (runbook) can ride with either half.

---

## Overall budget summary

| Section | Implementation lines | Test lines | Total |
|---|---|---|---|
| PR1 | ~130-150 | ~200-235 | ~330-385 |
| PR2a | ~295 | ~250-300 | ~545-595 |
| PR2b (worker/command half) | ~250 | ~230 | ~480 |
| PR2b (scenario/absorption/infra half) | ~0 (comments only) | ~600 | ~600 |
| **Total** | **~675-695** | **~1280-1365** | **~1955-2060** |

This is above the proposal's original 400-700 *implementation*-line
forecast when tests are excluded — implementation alone is ~675-695
lines, consistent with the forecast. Test volume (~1280-1365 lines) is
large because of the 8 individually-required crash/concurrency
scenarios, the 14-check command verification matrix, and the six
DB-constraint-plus-service-layer invariants each requiring direct tests
— all mandated explicitly by spec and by the user's task requirements,
not incidental. The four-way split (PR1 / PR2a / PR2b-worker+command /
PR2b-scenarios) keeps every individual reviewable unit under
~600 lines, none of which is capable of a real send until all four have
landed AND (B) is separately authorized.

---

## Traceability index (task → design section)

| Task range | Design section |
|---|---|
| PR1-T1, T2 | §9 (settings block) |
| PR1-T3, T4, T6 | §9 (gate shape and refusal codes) |
| PR1-T5, T8 | §12.1 |
| PR1-T7 | §12.3 (infrastructure only, no send code) |
| PR1-T9 | §14 (PR1 mechanical review criterion) |
| PR2a-T1 | §13 |
| PR2a-T2, T3 | §2.1, §2.2, §2.4 |
| PR2a-T4 | §2.5 point 1 |
| PR2a-T5 | §3, §4, §5, §6 |
| PR2a-T6, T7, T8, T9 | §2.3, §4, §6 |
| PR2b-T1 | §10, §14 |
| PR2b-T2, T34 | §10 |
| PR2b-T3, T31 | §10.1, §17 risk 2 |
| PR2b-T4 | §7, §14 |
| PR2b-T5 | §12.2 |
| PR2b-T6, T7, T8, T9-T21 | §8 |
| PR2b-T22-T29 | §6 (crash/restart table), §7 |
| PR2b-T30, T31 | §10.1 (V1 defect), §17 risk 2 |
| PR2b-T32, T33 | §11.2, §11.3 |
| PR2b-T35 | §14 ("no test performs a real HTTP call") |
| PR2b-T36 | §11.4, §5, §11.3 |

All 24 spec requirements across the three capabilities
(`bulk-v2-real-send-authorization`, `bulk-v2-send-state-machine`,
`bulk-v2-real-send-observability`) have at least one corresponding task
above; design.md §15 provides the requirement-level traceability this
index does not duplicate line-for-line.
