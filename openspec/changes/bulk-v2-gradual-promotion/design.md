# Design: Bulk V2 Gradual Promotion — Stage 1 Internal Pilot

Derived from `openspec/changes/bulk-v2-gradual-promotion/proposal.md` (Engram
`sdd/bulk-v2-gradual-promotion/proposal`). Architecture only — no code, no
production access, no flag activation in this phase.

## 1. Context

Two production canary cycles have run, both with a single dedicated technical
account (`bulk_v2_canary_runner`, pk=37) driven headlessly by
`ops/bulk_v2_canary_client.py`. Stage 1 changes three variables at once:
**many users instead of one**, **real staff identities instead of a technical
account**, and **a browser instead of a headless client**. Each of those three
crosses a boundary the existing machinery was built around, so the design must
verify the existing machinery per-dimension rather than assume it generalises.

`MAX_ROWS` stays `20`. Import-only stays absolute. V1 stays the default engine.

---

## 2. D0 — Central question: does Stage 1 require any `relay/` or `config/` change?

**Verdict: the proposal's hypothesis is CORRECT for the canary policy itself and
INCOMPLETE for the end-to-end path. `relay/` needs no change. `config/settings.py`
needs no change. Two other files DO require a change, and Stage 1 is not
executable without them.**

| File | Change required? | Evidence |
|---|---|---|
| `relay/services/bulk_v2_canary.py` | **NO** | `normalize_allowlist` is natively N-ary: `value.split(",")` (line 19), per-item loop (23–36), duplicate rejection via `len(set(...)) != len(...)` (37–38), wildcard rejection on `* ? [ ]` (25–26), positive-int coercion under `integer=True` (27–33). `evaluate_canary` uses membership, not equality: `request_id not in request_ids` (86), `int(user_id) not in user_ids` (88). Nothing in the module assumes cardinality 1. |
| `config/settings.py:161–166` | **NO** | `BULK_PROCESSING_V2_CANARY_REQUEST_IDS` / `_USER_IDS` are raw `env(...)` string passthroughs with `default=""`. No parsing, no split, no cardinality assumption. |
| `relay/api.py:421–459` | **NO** | Passes the two settings strings straight into `evaluate_canary` (428–433). No cardinality assumption anywhere in the V2 branch. |
| `ops/td02c_settings_gate.py` | **YES** | `evaluate_effective_settings` is hardcoded to exactly one entry per dimension: `expected_requests = (expected_request_id,)` (45), `expected_users = (expected_user_id,)` (46), `expected_request_raw = expected_request_id` (47), `expected_user_raw = str(expected_user_id)` (48). A 3–5 entry allowlist fails with both `request_allowlist_mismatch` (50–51) *and* `request_allowlist_not_canonical` (54–58), plus the user equivalents. Stage 1 activation **cannot be gate-verified** without an arity extension. |
| `config/templates/app/index.html` | **YES** | `newClientRequestId()` (251–256) returns `crypto.randomUUID()`, generated client-side per form mount, and is submitted as `client_request_id` (397). `evaluate_canary` requires *exact membership in the pre-registered env allowlist* (86). A random UUID can never be pre-registered, so **every browser-originated V2 attempt by a real staff member returns HTTP 409 `request_not_allowed`**. There is no UI field to supply an assigned token. This is the hard blocker. |
| `ops/bulk_v2_canary_client.py` | **NO — and deliberately not used** | `_assert_profile_matches_settings` (123–128) compares the profile against the *whole* env string with `!=`, so it is structurally single-valued. See D4: the client is not the Stage 1 execution vehicle, so it is left untouched. |

**Consequence for `sdd-tasks`: task category (A) is NOT documentation-only.** It
contains exactly two small production-code changes (`ops/td02c_settings_gate.py`,
`config/templates/app/index.html`), their tests, and documentation. It contains
**zero** changes to `relay/`, `config/settings.py`, or the canary policy.

---

## 3. D1 — Cohort mechanism

### Allowlist format

Both variables are canonical comma-separated strings with **no spaces, no
trailing comma, no duplicates, no wildcard characters**. Canonicality is not
stylistic: `ops/td02c_settings_gate.py:54–60` compares the raw env string with
`!=` against an expected literal, so ordering and spacing are load-bearing.

```
BULK_PROCESSING_V2_CANARY_USER_IDS=41,52,67
BULK_PROCESSING_V2_CANARY_REQUEST_IDS=stage1-c1-u41-01,stage1-c1-u41-02,...,stage1-c1-u67-05
```

- `_USER_IDS`: pilot user primary keys, **ascending numeric order**. 3–5 entries.
- `_REQUEST_IDS`: one token per *planned import*, grouped by user in the same
  ascending user order, then ascending sequence within a user.

### Token format and cardinality

`stage1-c<cycle>-u<pk>-<nn>` — e.g. `stage1-c1-u41-03`. Constraints, all derived
from existing code:

- must not contain `* ? [ ]` (`normalize_allowlist` line 25) nor `,` (the split
  delimiter) nor whitespace (`.strip()` would change it and break canonicality);
- ≤ 64 characters — `relay/api.py:385` truncates `client_request_id` to 64, and a
  truncated token would silently miss the allowlist;
- must be unique across the whole list (`normalize_allowlist` 37–38).

**One token equals exactly one import, permanently.** `client_request_id` is the
idempotency key: `relay/api.py:461–482` returns the existing `BulkSend` as a
duplicate for a re-used token. Therefore the number of tokens registered at
activation is the hard ceiling on imports for that window, and adding tokens
mid-window requires a `.env` edit plus a `django.service` restart.

**Mitigation: over-provision.** Register 5 tokens per pilot user up front. Unused
tokens are inert — they authorise nothing on their own, because
`evaluate_canary` requires *both* request-id and user-id to match (86, 88).

### Multi-entry correctness

`normalize_allowlist` behaviour for a 3–5 entry list, read directly from the
source rather than extrapolated from the proven single-entry case:

| Input | Result | Why |
|---|---|---|
| `"41,52,67"`, `integer=True` | `((41, 52, 67), None)` | split → strip → `int()` → positive check → no duplicates |
| `"41, 52"` | `((41, 52), None)` **but gate fails** | `.strip()` normalises the space, so `evaluate_canary` accepts it; `td02c_settings_gate.py:59` then reports `user_allowlist_not_canonical` on the raw string. Fail-closed, as intended. |
| `"41,52,41"` | `((), "canary_config_invalid")` | duplicate rejection (37–38) |
| `"41,,52"` | `((), "canary_config_invalid")` | empty item (25) |
| `"41,5*"` | `((), "canary_config_invalid")` | wildcard (25) |
| `"41,0"` / `"41,-2"` | `((), "canary_config_invalid")` | non-positive (32–33) |

A single invalid entry poisons the **entire** allowlist — the function returns
`()`, and `evaluate_canary` then short-circuits to `canary_config_invalid`
(69–72) for *every* user, not just the malformed one. This is the desired
fail-closed shape and it must be stated in the runbook: a typo in one pilot user's
entry disables the whole pilot rather than partially enabling it.

### Cohort permissioning

No new permission model. Pilot users must already satisfy
`relay/services/operator_permissions.py::can_operate_bulk_sends` (20–29):
authenticated, `is_active`, `is_staff`, and holding `relay.change_bulksend`
— i.e. members of the existing `Operadores UI` group (`OPERATOR_GROUP_NAME`,
line 11). Allowlist membership is an *additional* gate on top of that, never a
substitute. Onboarding a pilot user never grants a permission they lacked.

---

## 4. D2 — Delivering the assigned request-id to a real operator

**Decision (R1): add one optional text input to `config/templates/app/index.html`,
rendered only when the V2 engine option is both available and selected.** When
non-empty, it replaces the generated UUID for that one submission; otherwise the
existing UUID behaviour is unchanged. Roughly ten lines, additive, no server change.

Rejected alternatives:

- **R2 — relax `evaluate_canary` so user-id membership alone suffices.** Rejected:
  it deletes one of the two independent allowlist dimensions, removes the
  per-import idempotency ceiling that currently bounds pilot blast radius, and
  would force a matching relaxation of the gate's canonicality check. It trades
  the proven firewall for convenience.
- **R3 — have pilot users run `ops/bulk_v2_canary_client.py`.** Rejected under D4.
- **R4 — instruct operators to inject the token via browser devtools.** Rejected:
  unauditable, unteachable, and not a runbook.

The firewall is unchanged by R1: the server still requires exact membership in a
pre-registered, gate-verified allowlist. The UI change only lets an authorised
operator *express* the token they were assigned.

### Accepted exposure: the V2 toggle is visible to all operators

`relay/api.py:363–369` computes `capabilities.bulk_processing_v2_create` as
`BULK_PROCESSING_ENGINE_V2 and can_operate_bulk_sends(user)` — **it does not
consult the allowlist**. While the pilot window is open, every `Operadores UI`
member sees the V2 engine option, not just pilot users.

This is pre-existing (both prior canaries ran with the engine flag globally on)
and the failure mode is clean: a non-pilot operator selecting V2 receives HTTP
409 with `user_not_allowed` and the message `El usuario no esta autorizado.`,
and **no row is created** — the check runs at `relay/api.py:456–459`, before
`BulkSend.objects.create` at 509.

**Decision: accept it; do not change `relay/api.py`.** Mitigation is a
pre-window notice to all operators (part of the runbook). Tightening the
capability flag to require allowlist membership is registered as an optional
Stage 2 item, not Stage 1 work — it would be the only `relay/` change in the
whole initiative and buys UX polish, not safety.

---

## 5. D3 — Settings-gate arity extension

**Decision: extend `evaluate_effective_settings` / `evaluate_django_settings` to
accept sequences of expected request-ids and user-ids, preserving the existing
single-value call shape.**

Shape:

- accept `expected_request_ids: Sequence[str]` and `expected_user_ids: Sequence[int]`;
- keep today's scalar parameters as accepted single-value spellings so every
  existing caller and test is behaviour-compatible;
- `expected_requests` / `expected_users` become the full declared tuples;
- `expected_request_raw` / `expected_user_raw` become `",".join(...)` over the
  declared values **in declared order** — preserving exact-string canonicality,
  now over N values instead of one;
- `expect_active=False` keeps producing `()` and `""` for both dimensions,
  unchanged.

### Why this is provably deployment-neutral

`ops/td02c_deployment_runner.py:249` calls
`evaluate_django_settings(settings, expect_active=False)` with **no** expected-id
arguments. Under `expect_active=False`, lines 45–48 discard the expected values
entirely (`()` / `""`). The extension therefore cannot alter the deployment
preflight's behaviour in either direction — it is unreachable from that path.

Rejected alternative: **skip gate verification for a multi-user pilot.** Rejected
outright. The fail-closed gate is the single mechanism that made both prior
canaries safe. Dropping it for the run with the *larger* blast radius inverts the
risk profile.

Out of scope here: parametrising the `max_rows != 20` check (line 67). See §14.

---

## 6. D4 — Identity model: real staff accounts, own passwords, no ephemeral credentials

The proposal did not resolve this. **Decision: pilot users authenticate as
themselves, in a browser, with their own existing passwords. No shared account,
no technical account, no credential file, no ephemeral-credential dance, and
`ops/bulk_v2_canary_client.py` is NOT used for Stage 1.**

Reasoning:

1. **It is the hypothesis under test.** Stage 1 exists to observe real operators
   doing real work. Re-running a technical account merely re-proves the canary.
2. **Credential handling would be a security regression.** `run_canary`
   (`ops/bulk_v2_canary_client.py:255–263`) requires a `credential_file` holding
   the account password, validated by `validate_credential_file` (322) and
   shredded by `delete_exact_file` (393). Applying that to 3–5 real staff members
   means collecting and writing their personal passwords to disk. There is no
   justification: the ceremony exists only because a *headless* client must
   authenticate. A human with a browser has no such need.
3. **Attribution and abort granularity require distinct identities.**
   `evaluate_canary` gates on `request.user.pk` (`relay/api.py:441`), and
   `BulkSend.scheduled_by` records the acting user (`relay/api.py:522`). Real
   accounts give per-user evidence and make per-user abort (remove one pk from
   `_USER_IDS`) meaningful. A shared account collapses both.
4. **The client is structurally single-user anyway** (`_assert_profile_matches_settings`,
   123–128, raw string equality against the whole env value). Adapting it would
   mean modifying a hardened, identity-validated module to serve a use case it
   was explicitly designed against.

**Role separation this creates.** The operator produces the import; a separate
authorised ops engineer performs gate verification, evidence capture, and
disposition on the host. Pilot users never receive shell access and never run
`ops/` tooling. This separation is a design requirement, not an accident of the
decision.

---

## 7. D5 — PASS threshold

Grounded in the actual baseline: **two** production canary executions ever. The
threshold below is roughly 6× that baseline — a real step, not an aspiration.

Stage 1 is declared **PASS** only when **all** of the following hold:

| # | Criterion | Rationale |
|---|---|---|
| 1 | ≥ 12 successful pilot imports total | 6× the current production baseline |
| 2 | ≥ 3 distinct pilot users participated, each with ≥ 3 successful imports | prevents one enthusiastic user carrying the whole sample |
| 3 | ≥ 1 `ready` **and** ≥ 1 `ready_with_errors` outcome per participating user | exercises both branches of `bulk_import.py:581–585`; the all-valid path alone proves nothing about validation |
| 4 | ≥ 3 completed activation cycles, each window ≤ 3 business days | see the deployment-freeze constraint below |
| 5 | ≥ 10 business days elapsed from first activation to final deactivation | surfaces time-dependent and cross-deploy issues |
| 6 | 0 full-pilot aborts (see D6) | any firewall breach voids the stage |
| 7 | ≤ 1 per-user removal, and only for a demonstrably user-specific cause | more than one suggests a systemic defect, not a user issue |
| 8 | 100 % of executions satisfy the parity oracle (D7) and report `reconciled=True` | parity is the whole point |
| 9 | Every cycle ended with `(jobs, v2, ledger) == (0, 0, 0)` and flags off | disposition and rollback proven repeatedly, not once |

Anything short of all nine is **not PASS**. There is no partial promotion — the
same rule the existing canary section already states (`ops/README.md:192–193`).

### Why cycles, not a single long window: the pilot window is a deployment freeze

`ops/td02c_deployment_runner.py:534–538` (`_validate_operational_gates`) raises
`unexpected_active_work` unless `(jobs, v2, ledger) == (0, 0, 0)`, and `_django_state()`
(241–257) additionally requires `evaluate_django_settings(..., expect_active=False)`
to pass. While flags are on **or** any V2 row exists, **every** `ops/` deployment
fails closed.

An open pilot window is therefore a deployment freeze. This is the dominant cost
of Stage 1 and the proposal did not state it. It is why the design prescribes
several short windows rather than one long one: each window is bounded at 3
business days, and cycles are separated by at least one deployment-capable
interval so ordinary delivery is never blocked for long.

---

## 8. D6 — Entry, promote, and abort criteria

### Entry (per user, before being added to `_USER_IDS`)

- `is_active`, `is_staff`, and `can_operate_bulk_sends(user) is True`;
- member of `Operadores UI`;
- explicit owner authorisation naming the user;
- the user has been briefed: import-only, ≤ 20 rows, one token per import, rows
  will be deleted after the cycle.

### Entry (per cycle, before flipping any flag)

- `evaluate_django_settings(settings, expect_active=False)` → `allowed=True`;
- `(jobs, v2, ledger) == (0, 0, 0)` — no residue from the prior cycle;
- the exact canonical raw values for both env variables are written down, and the
  prior values recorded verbatim so rollback is a literal revert;
- no `ops/` deployment is in flight or scheduled inside the window.

### Promote (per execution)

An execution counts toward the PASS threshold only when all hold:

- gate reported `canary_settings_active`;
- the operator received HTTP 201 with `import_status` in `{ready, ready_with_errors}`;
- application log shows `bulk_v2_canary decision=canary_allowed` for the matching
  request fingerprint;
- `BackgroundJob.objects.filter(state__in=("queued","running")).count()` delta `== 0`;
- `EmailMessage.objects.count()` delta `== 0`;
- `BulkSend` delta `== +1`, `BulkSend(engine_version="v2")` delta `== +1`,
  `BulkSendRecipient` delta `>= 1`;
- the parity oracle (D7) matched;
- `get_bulk_import_progress(bulk, reconcile=True).reconciled is True`.

### Abort — two distinct blast radii

| Trigger | Blast radius | Action |
|---|---|---|
| Sustained validation or usability defect reproducible **only** for one user's data/workflow | **Per-user** | Remove that pk from `_USER_IDS` **and** their unused tokens from `_REQUEST_IDS`, restart, re-verify `expect_active=True` against the reduced canonical list. The pilot continues for the rest. |
| `firewall_breach` — nonzero jobs delta or nonzero `EmailMessage` delta | **Full pilot** | All four flags off, restart, verify `expect_active=False`, dispose, escalate. No retry. |
| `unexpected_row_delta` — `BulkSend` / `BulkSend v2` / `BulkSendRecipient` deltas outside the expected shape | **Full pilot** | as above |
| Gate returns any non-empty `reasons` at any point | **Full pilot** | as above |
| A V2 `BulkSend` reaches `import_status=error` for input the operator judged valid | **Full pilot**, pending diagnosis | as above |
| Any enqueue, send, or scheduled send attributable to a V2 bulk | **Full pilot** | as above; this is the structural firewall failing |

Classification vocabulary is reused verbatim from
`ops/bulk_v2_canary_client.py::ERROR_CODES` (60–72) —
`firewall_breach`, `unexpected_row_delta`, `disposition_required`,
`settings_gate_failed` — even though the client itself is not executed. Reusing
the taxonomy keeps Stage 1 evidence comparable with the two prior canary records.

A per-user removal never converts into a full abort implicitly, and a full abort
never degrades into a per-user removal. The distinction is the operator's single
most consequential judgement, so it is written before the window opens, not
during an incident.

---

## 9. D7 — Evidence per execution

**Decision: no new logging, no new `ops/` module. Existing fields are sufficient.**

### What the existing logs already carry

`relay/api.py:450–455`:
```
bulk_v2_canary decision=%s request=%s rows=%s external_calls=0
```
→ `decision.code`, `sha256(client_request_id)[:12]`, pre-import row count, and the
zero-external-calls assertion.

`relay/api.py:562–572`:
```
bulk_v2_import bulk_id=%s engine=v2 result=%s rows=%s ledger=%s
duration_ms=%s spool_bytes=%s background_jobs=0 external_calls=0
```
→ `BulkSend.pk`, `bulk.import_status` (**the operator-visible import status**),
`result.total_rows`, persisted occurrence count, duration, upload size, and the
two firewall assertions.

`relay/services/bulk_import.py:246–263` adds two `bulk_v2_spool` lines with row
count, spool bytes, `mode=0600`, and the resulting `import_status`.

### The one gap, and why it needs no code

`valid_rows` / `invalid_rows` appear in **neither** log line. Do not attempt to
derive them from `ledger=`: that field is `bulk.recipient_occurrences.count()`
(`relay/api.py:569`), and `_persist_spool` (`bulk_import.py:564–575`) persists a
`BulkSendRecipient` for **every** spooled row, valid or not. `ledger` equals
*total*, not *valid*.

Both counts are nonetheless available from two independent, already-existing
sources:

1. **The HTTP 201 response body the operator sees.** `_bulk_payload`
   (`relay/api.py:231–236`) returns
   `import.{status,total_rows,valid_rows,invalid_rows,pending_rows}`.
   Since parity is defined over the *operator-visible* outcome, this response
   **is** the primary artefact — not a proxy for it.
2. **The persisted `BulkSend` row**, read before disposition:
   `imported_rows`, `valid_rows`, `invalid_rows`, `import_status`
   (`bulk_import.py:577–593`).

Adding `valid=` / `invalid=` to `relay/api.py:562–572` would be ergonomic, not
necessary — and it would be a `relay/` change that D0 otherwise avoids entirely.
**Rejected for Stage 1; registered as optional Stage 2 ergonomics.** This
answers proposal open question 4: **no parity signal is missing.**

### Attribution across a multi-user cohort

The canary log line carries a request fingerprint but no user id. Attribution is
nonetheless exact and privacy-preserving:

- tokens are assigned 1:1 to a user, so `sha256(token)[:12]` identifies the user
  indirectly without any raw pk reaching a log;
- `BulkSend.scheduled_by_id` (`relay/api.py:522`) gives authoritative attribution
  from the persisted row, read once per cycle before disposition.

No logging change is required for attribution either.

### The parity oracle

Proposal definition: *identical validation outcome and identical operator-visible
import status for equivalent input.*

A direct row-level V1-versus-V2 comparison is **structurally impossible in Stage 1**:
`get_bulk_import_progress` returns all zeros for any non-V2 engine
(`relay/services/bulk_progress.py:22–23`), because V1 has no import phase at all —
it validates inline during send, and sending is out of scope. This is a finding,
not an omission: it must not be silently designed around.

The operative Stage 1 oracle is therefore **pre-declared expectation matching**:

1. Before submitting, the operator writes down the expected `valid_rows`,
   `invalid_rows`, and `import_status` for their CSV.
2. After submitting, the response body must match all three exactly.
3. Independently, `get_bulk_import_progress(bulk, reconcile=True)` must return
   `reconciled=True` (`bulk_progress.py:46–50`) — the denormalised counters agree
   with the ledger recomputed from `BulkSendRecipient.status`.
4. `import_status` must equal `ready_with_errors` iff `invalid_rows > 0`
   (`bulk_import.py:581–585`).

Step 3 is a free, already-implemented integrity oracle. Step 1 must be recorded
**before** submission, or it is rationalisation rather than evidence.

Full V1/V2 row-level equivalence is registered as a **Stage 2 prerequisite**: it
requires a V1 send, which is out of scope.

### Evidence record per execution

A JSON object with exactly these fields, whose schema belongs in `ops/README.md`
(no new module — every field is a one-line read-only ORM query or a copy from the
response body):

```
cycle, request_fingerprint, user_pk, bulk_id,
gate_before, gate_after,
expected: {valid_rows, invalid_rows, import_status},
observed: {total_rows, valid_rows, invalid_rows, import_status, reconciled},
deltas:   {bulk_sends, bulk_sends_v2, recipients, jobs, messages},
duration_ms, disposition: {deleted_recipients, deleted_bulk_sends, media_removed},
classification
```

**Never record** the raw `client_request_id` or recipient data — the existing
fingerprint-only convention (`ops/README.md:210–213`,
`ops/bulk_v2_canary_client.py:295–299`) applies unchanged. `user_pk` **is**
recorded, because per-user abort is impossible without it and it is not a secret.

### Why no new `ops/` evidence module

The proposal listed one as "possibly new, only if design proves it needed".
It is not needed. Every field is a single read-only query run once per cycle
boundary by an ops engineer who already has host access. A module would inherit
this repository's module-identity ceremony (cf.
`validate_client_module_identity`, `ops/bulk_v2_canary_client.py:402–449`), its
own test suite, and its own review surface — for no capability gain. Documented
snippets in `ops/README.md` are the proportionate mechanism.

---

## 10. D8 — Activation / deactivation / rollback runbook

Same flags-only pattern proven twice, adapted for a cohort. No code is deployed
or modified by activation or rollback.

| # | Step | Verification before continuing |
|---|---|---|
| 0 | Notify all `Operadores UI` members that the V2 option will be visible but authorised only for named pilot users (§4) | notice sent |
| 1 | Read effective state | `evaluate_django_settings(settings, expect_active=False)` → `allowed=True` |
| 2 | Confirm no residue | `(jobs, v2, ledger) == (0, 0, 0)` |
| 3 | Compose canonical raw values: `_USER_IDS` ascending; `_REQUEST_IDS` grouped by user, over-provisioned to 5 tokens/user | no spaces, no duplicates, no wildcards, each token ≤ 64 chars |
| 4 | Apply the `.env` edit (operator): `BULK_PROCESSING_ENGINE_V2=True`, `BULK_PROCESSING_V2_CANARY_ENABLED=True`, both allowlists set. `BULK_PROCESSING_V2_CANARY_MAX_ROWS` stays `20`; `BULK_PROCESSING_V2_ALLOW_EXTERNAL_TEMPLATE_LOOKUP` stays `False` | prior line values recorded verbatim |
| 5 | Restart `django.service` | existing `td02c_worker_gate` pre/post checks + `validate_readiness_layers` PASS |
| 6 | Prove ON | `evaluate_django_settings(settings, expect_active=True, expected_request_ids=[...], expected_user_ids=[...])` → `allowed=True`, `code="canary_settings_active"` |
| 7 | Distribute one token per planned import to each pilot user | tokens delivered out of band; raw tokens never logged |
| 8 | Pilot window opens: users import via the browser, pasting their assigned token | per-execution promote criteria (D6) checked for each |
| 9 | Capture evidence for every execution | record complete per D7, **before** any deletion |
| 10 | Close window: `.env` → `False` / empty, restart | `evaluate_django_settings(settings, expect_active=False)` → `allowed=True` |
| 11 | Dispose (D9) | `(jobs, v2, ledger) == (0, 0, 0)`; media files removed |

Steps 10 and 11 always run — on success or abort. Abort at any failed step.

**Flags first, always.** `_django_state()` hardcodes `expect_active=False`
(`ops/td02c_deployment_runner.py:249`), so while any flag is active every future
deployment preflight fails closed. Step 10 is never optional and never deferred
past the window, independently of whether step 11 has happened.

### Rollback layers

| Layer | Trigger | Mechanism | Verification |
|---|---|---|---|
| Flags | any abort after step 4, or normal window close | literal `.env` revert to the recorded prior values + restart | `expect_active=False` → `allowed=True` |
| Cohort (partial) | per-user abort | remove that pk and its unused tokens; restart | `expect_active=True` against the **reduced** canonical list → `allowed=True` |
| Data | whenever any V2 row exists | D9 | `(jobs, v2, ledger) == (0, 0, 0)` |
| Code | only if (A) shipped and is implicated | revert the `ops/td02c_settings_gate.py` and/or `config/templates/app/index.html` commit | existing runner evidence |

V1 is unaffected throughout: `relay/models.py:206–216` blocks `engine_version`
mutation once an import has started.

---

## 11. D9 — Disposition

Retention is not viable, for the reason already documented at
`ops/README.md:215–232` and confirmed at `ops/td02c_deployment_runner.py:534–538`:
any retained V2 `BulkSend` or `BulkSendRecipient` row makes `(jobs, v2, ledger) != (0,0,0)`
and **permanently** blocks every future `ops/` deployment. Rows kept "as evidence"
are an outage in waiting; the evidence record from D7 is the durable artefact.

**Decision: dispose per cycle, after evidence capture, never before.** Confirms
the proposal's recorded assumption.

Order, scoped and identity-verified per pilot `BulkSend`:

1. `BulkSendRecipient` rows for that `BulkSend`;
2. the parent `BulkSend` (`engine_version="v2"`), matched by its exact
   `client_request_id` token **and** `scheduled_by_id` — both, never one alone;
3. the `recipients_file` media artefact under `bulk_recipients/` — deleting the
   row does not remove the file from disk.

Then verify `(jobs, v2, ledger) == (0, 0, 0)`, restoring deployability.

No `EmailMessage` row should exist for any pilot execution. If one does, it is a
`firewall_breach` to escalate under D6 — never a disposition item to quietly delete.

---

## 12. D10 — Why the pilot cannot break other deployments (binding constraint)

`ops/td02c_settings_gate.py:67` evaluates

```python
if type(max_rows) is not int or max_rows != 20:
    reasons.append("max_rows_mismatch")
```

**unconditionally** — outside any `expect_active` branch — and
`ops/td02c_deployment_runner.py:241–257` (`_django_state`) invokes the gate before
every `preflight-only` / `deploy-only` run. Raising `BULK_PROCESSING_V2_CANARY_MAX_ROWS`
above 20 would therefore fail-close **all** future `ops/` deployments, not merely
V2 work.

Stage 1 keeps `MAX_ROWS = 20`, so this failure mode cannot trigger.

> **Design constraint, binding for the entire duration of Stage 1:**
> no engineer may change `BULK_PROCESSING_V2_CANARY_MAX_ROWS` in any environment,
> and no engineer may modify the `max_rows != 20` check in
> `ops/td02c_settings_gate.py`. Raising the cap is a Stage 2 prerequisite that
> must be preceded by parametrising that check — designing that parametrisation
> is explicitly out of scope here (§14).

The D3 arity extension does not weaken this: it touches only the request/user
allowlist dimensions, and under `expect_active=False` those expectations collapse
to `()` / `""` regardless of arguments (lines 45–48), leaving the deployment path
behaviourally identical.

---

## 13. (A) support artefacts versus (B) production execution

**Completing (A) is never authorisation for (B).** No task may bundle (B) into
apply. Carried forward from the proposal and made concrete here.

### (A) — repo artefacts `sdd-apply` may produce

| Path | Nature |
|---|---|
| `openspec/changes/bulk-v2-gradual-promotion/{proposal,design,tasks}.md` | docs |
| `openspec/changes/bulk-v2-gradual-promotion/specs/bulk-v2-pilot-cohort/spec.md` | spec |
| `openspec/changes/bulk-v2-gradual-promotion/specs/bulk-v2-pilot-evidence/spec.md` | spec |
| `ops/td02c_settings_gate.py` | **code** — D3 arity extension, backward compatible |
| `ops/tests/test_td02c_settings_gate.py` | **tests** — N-entry canonical PASS; wrong order → `*_not_canonical`; duplicate / wildcard / non-positive → `*_mismatch`; `expect_active=False` unaffected by expected-id arguments; existing single-value cases still pass unchanged |
| `config/templates/app/index.html` | **code** — D2 optional pilot request-id input, shown only when V2 is available and selected |
| `relay/tests/test_bulk_v2_canary.py` | **tests** — multi-entry allowlist admits each listed (user, token) pair and rejects a non-listed user and a non-listed token; one malformed entry disables the whole list |
| `ops/README.md` | docs — new "Bulk Processing Engine V2 Stage 1 internal pilot (production)" section: cohort format, token format, runbook (D8), criteria (D6), evidence schema and ORM snippets (D7), disposition (D9), the `MAX_ROWS` constraint (D10) |
| `.env.example` (only if it already documents these flags) | docs — cohort-shaped example values |

Not produced: any change to `relay/api.py`, `relay/services/bulk_v2_canary.py`,
`relay/services/bulk_import.py`, `config/settings.py`,
`ops/bulk_v2_canary_client.py`, or the `max_rows` check.

### (B) — production execution, requiring separate explicit owner authorisation

- selecting and naming the 3–5 pilot users, and obtaining their consent;
- generating and distributing request-id tokens;
- editing the production `.env` (any of the four flags);
- restarting `django.service`;
- running `evaluate_django_settings` against production settings;
- pilot users performing imports;
- reading production data for evidence capture;
- executing the disposition deletes;
- declaring Stage 1 PASS or abort.

---

## 14. Explicitly out of scope — do not design here

- **V2 send capability.** Structurally excluded: `relay/api.py:548–573` (the V2
  branch calls only `BulkImportService(...).import_file()`; enqueue is a mutually
  exclusive `elif`), `relay/services/bulk_processing.py:39–42`,
  `relay/management/commands/process_bulk_scheduled.py:18–22,40–44`.
- Any send-worker change; V2 scheduled sends; V2 as default engine; V1 deprecation.
- **`MAX_ROWS` parametrisation in `ops/td02c_settings_gate.py`** — registered as a
  **named Stage 2 prerequisite only**. Its implementation is not designed here.
- Full row-level V1/V2 equivalence comparison — Stage 2 prerequisite (requires a
  V1 send). See D7.
- Tightening `capabilities.bulk_processing_v2_create` to consult the allowlist —
  optional Stage 2 item. See D2.
- Adding `valid=` / `invalid=` to the `bulk_v2_import` log line — optional Stage 2
  ergonomics. See D7.
- Activating flags, running imports, or any production access during this change.

---

## 15. Residual risks and assumptions

| # | Item | Status |
|---|---|---|
| 1 | The pilot window is a deployment freeze (§7). Bounded at 3 business days per cycle, but it is a real, recurring delivery cost the proposal did not state. | Accepted, mitigated by short cycles |
| 2 | The V2 toggle is visible to all `Operadores UI` members during a window (§4). Clean 409, no data created, but it will generate support questions. | Accepted, mitigated by the step-0 notice |
| 3 | Token exhaustion mid-window forces a `.env` edit plus restart. | Mitigated by over-provisioning 5 tokens/user |
| 4 | One malformed allowlist entry disables the pilot for everyone (§3). | Intentional fail-closed; must be in the runbook |
| 5 | Parity is measured against operator pre-declaration, not against V1 (D7). Weaker than true differential testing. | Accepted for Stage 1; true differential comparison is a Stage 2 prerequisite |
| 6 | The D2 UI change is untested in production and sits on the operator's critical path. | Additive and inert when the field is empty; covered by (A) tests |
| 7 | Pilot user identities are not yet authorised. | External dependency; blocks (B), not (A) |
| 8 | Real staff will create and then delete production rows under their own identity. | Bounded at 20 rows, import-only, deleted per cycle, identity-verified |
