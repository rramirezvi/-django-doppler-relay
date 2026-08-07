# Tasks: Bulk V2 Gradual Promotion — Stage 1 Internal Pilot

Scope reminder (design §13, binding): completing every task below is **never
authorization** to run the pilot. This file has two sections. **Section (A)**
is the only thing `sdd-apply` may execute — it produces reviewable repo
artifacts (code, tests, docs). **Section (B)** is a non-actionable reference
list of production-execution steps; it exists only so the runbook written in
Task 5 has somewhere authoritative to point future authorized operators to.
No task in Section (A) selects pilot users, edits production `.env`, restarts
`django.service`, runs the gate against production, or touches production
data. If any future agent is tempted to treat "(A) tasks are all checked" as
a green light for (B), that reading is wrong by design.

Test convention followed (per `sdd-init` obs #316, `strict_tdd=true`,
`openspec/config.yaml` `apply.tdd: true`, and the same discipline already
used in `activate-bulk-v2-canary/tasks.md`): each production-code change is
paired with a test task that runs and fails (RED) before the implementation
task, then passes (GREEN) after. Two exceptions, both explicit per design D0:

- The `relay/services/bulk_v2_canary.py` multi-entry behavior already exists
  (`normalize_allowlist` is natively N-ary — design D0/§3). Task 3 below adds
  **confirmation tests**, not RED-first tests: they are expected to pass
  immediately against unmodified code, because no `relay/` change is made or
  needed. This is evidence for D0's verdict, not a TDD violation.
- `config/templates/app/index.html` is a browser-rendered template with no
  existing automated test harness in this repo (no JS test runner detected,
  no prior test exercises this file — verified: no match for
  `templates/app` under `relay/tests/`). Task 4 is verified structurally
  (manual render/diff review, not a new test module), matching the design's
  own artifact table (§13), which lists no dedicated test file for this
  change.

## (A) Implementation tasks

### Phase 0 — Settings-gate arity extension (D3, sequential pair)

- [x] **1. [Test-first] Extend `ops/tests/test_td02c_settings_gate.py`**
      Add cases per design D3 / §13's exact enumeration, keeping every
      existing test method unmodified:
      - N-entry canonical PASS: `expected_request_ids=("stage1-c1-u41-01",
        "stage1-c1-u41-02")` / `expected_user_ids=(41, 52)` matching an
        allowlist of `"stage1-c1-u41-01,stage1-c1-u41-02"` /
        `"41,52"` → `allowed=True`.
      - Wrong order → `*_not_canonical`: same set, declared/joined in a
        different order than the raw env string → `request_allowlist_not_canonical`
        / `user_allowlist_not_canonical`.
      - Duplicate, wildcard, or non-positive entry in the expected sequence
        → `*_mismatch` (mirrors the existing single-value duplicate/wildcard/
        non-positive cases, now exercised through the sequence parameters).
      - `expect_active=False` is unaffected by supplying
        `expected_request_ids` / `expected_user_ids`: they still collapse to
        `()` / `""` regardless of what is passed (design's proof that this is
        deployment-neutral, since `ops/td02c_deployment_runner.py:249` calls
        with `expect_active=False` and no expected-id arguments at all).
      - Every existing single-value test in this file (scalar
        `expected_request_id` / `expected_user_id`, defaults, keyword-only
        enforcement) keeps passing unmodified — do not delete or rewrite any
        current test method.
      Run: `python -m unittest ops.tests.test_td02c_settings_gate -v` → RED
      (new N-entry cases fail; module not yet extended).

- [x] **2. [Impl, after 1] Extend `ops/td02c_settings_gate.py`**
      Add `expected_request_ids: Sequence[str] = ()` and
      `expected_user_ids: Sequence[int] = ()` to
      `evaluate_effective_settings` (threaded through by
      `evaluate_django_settings`, same optional-keyword shape as the existing
      `expected_request_id` / `expected_user_id`). Behavior, per design D3:
      - `expected_requests` / `expected_users` become the full declared
        tuples — today's scalar params fold in as a single-value spelling of
        the same sequence (e.g. `expected_request_ids=() ` falls back to
        `(expected_request_id,)` when only the scalar is given, preserving
        every current call site unchanged);
      - `expected_request_raw` / `expected_user_raw` become
        `",".join(...)` over the declared values **in declared order**;
      - `expect_active=False` still discards all expected values to `()` /
        `""` for both dimensions, unchanged from today.
      Run: `python -m unittest ops.tests.test_td02c_settings_gate -v` →
      GREEN, all prior tests unchanged.

### Phase 1 — Independent of Phase 0 and of each other (parallel)

- [x] **3. [Confirmation tests, no `relay/` code change] Extend
      `relay/tests/test_bulk_v2_canary.py`**
      Add test methods per design §13's exact enumeration:
      - a multi-entry allowlist (3 user/token pairs) admits each listed
        `(user_id, client_request_id)` pair — `evaluate_canary(...).allowed
        is True` for every listed pair;
      - the same allowlist rejects a user id not on the list
        (`user_not_allowed`) and rejects a `client_request_id` not on the
        list (`request_not_allowed`), even when the other dimension is
        valid;
      - one malformed entry in either allowlist (duplicate, wildcard,
        non-positive) disables the **entire** list —
        `canary_config_invalid` for every user, not only the malformed one
        (mirrors the existing `test_duplicates_wildcards_and_nonpositive_limit_are_invalid`
        pattern, extended to a multi-entry list).
      Expected outcome: **GREEN on first run**, no implementation task
      follows — this proves design D0's verdict that
      `relay/services/bulk_v2_canary.py` needs no change for Stage 1.
      Run: `python manage.py test relay.tests.test_bulk_v2_canary -v 2`.

- [x] **4. [Impl, structural verification] Add pilot request-id readonly
      field to `config/templates/app/index.html`, sourced from the URL
      fragment**
      Superseded from design D2/R1's original free-text-input sketch by an
      explicit orchestrator/user decision after design completed: the field
      is **not editable**. On page load, read `window.location.hash` for a
      `pilot_token` entry (format: `#pilot_token=<token>`, standard
      fragment key=value parsing, URL-decoded). Rendered only when
      `v2Available` is true **and** `form.engine_version === "v2"` **and**
      a non-empty `pilot_token` was found in the fragment, placed next to
      the existing "Motor" (`engine_version`) selector (around line
      472–488), as a **readonly** input displaying the decoded token. If
      present, use it verbatim (post URL-decode) as `client_request_id` at
      submit time, replacing the `newClientRequestId()`-generated UUID for
      that one submission. If the fragment is absent, or present but empty,
      the field does not render and today's `crypto.randomUUID()` behavior
      is exactly unchanged. No server-side change (`relay/api.py`
      untouched — the server still requires exact allowlist membership via
      `evaluate_canary()` regardless of where the token came from; the
      fragment is a distribution/UX choice, never a security boundary).
      Rationale for fragment over query string (`?pilot_token=`): URL
      fragments are never transmitted to the server in the request line,
      never appear in server access logs, and are stripped from the
      `Referer` header by browsers — a query parameter would leak the
      token into all three. ~15 lines, additive.
      Verification: no JS test runner exists in this repo for this file
      (confirmed at design time). Cover the following with the most
      precise mechanism available — a Node-executable unit test against
      the extracted parsing/fallback function if it can be isolated
      cleanly, otherwise exact, individually-checkable manual verification
      steps recorded in the PR description (state explicitly which route
      was taken and why):
      1. valid non-empty fragment (`#pilot_token=stage1-c1-u41-01`) → field
         renders readonly with the decoded value, that value is sent as
         `client_request_id`;
      2. fragment absent entirely → field does not render, submit uses
         `newClientRequestId()` exactly as before;
      3. fragment present but empty (`#pilot_token=`) → treated identically
         to absent (field does not render, UUID fallback);
      4. URL-encoded value in the fragment (e.g. a token containing no
         encodable chars per the design's charset, but the decode step
         itself must be exercised, e.g. `%2D` for `-` or similar) decodes
         correctly before being used/displayed;
      5. the rendered input has the `readonly` attribute and no code path
         permits editing its value before submit;
      6. absence of `pilot_token` produces byte-for-byte the same submit
         behavior as before this task (fallback to UUID), verified against
         the pre-change code path;
      7. a user who is not a pilot (no `pilot_token` in their URL, whatever
         their `v2Available`/`engine_version` state) sees zero behavior
         change from before this task.

### Phase 2 — Documentation (can be drafted in parallel with Phase 0/1, finalize after)

- [x] **5. Doc: `ops/README.md` new section — "Bulk Processing Engine V2
      Stage 1 internal pilot (production)"**
      Add as a new `##` section (matching the existing `## Bulk Processing
      Engine V2 canary activation (production)` section at line 73, placed
      immediately after it, before `## Phases`). Content, sequenced to match
      the design:
      - **Cohort/token format (D1):** `_USER_IDS` ascending numeric order,
        3–5 entries; `_REQUEST_IDS` grouped by user then ascending sequence,
        token shape `stage1-c<cycle>-u<pk>-<nn>`, ≤ 64 chars, no `* ? [ ]`,
        no comma, no whitespace; over-provision 5 tokens/user; state the
        canonical-string constraint (spacing/order is load-bearing per the
        gate) and the fail-closed rule that one malformed entry disables the
        whole allowlist.
      - **Runbook (D8):** the 12-step table (notify → read state → confirm
        no residue → compose canonical values → apply `.env` → restart →
        prove ON via the new `expected_request_ids`/`expected_user_ids`
        gate call → distribute tokens → window open → capture evidence →
        close window → dispose), with the "flags first, always" rule and the
        three-layer rollback table (flags / cohort partial / data / code).
      - **Entry/promote/abort criteria (D6):** per-user and per-cycle entry
        checklist; per-execution promote checklist; the two-blast-radius
        abort table (per-user removal vs. full-pilot abort) with the
        reused `ERROR_CODES` vocabulary from
        `ops/bulk_v2_canary_client.py`.
      - **Evidence schema + ORM snippets (D7):** the JSON field list
        (`cycle, request_fingerprint, user_pk, bulk_id, gate_before,
        gate_after, expected{...}, observed{...}, deltas{...}, duration_ms,
        disposition{...}, classification`), the parity oracle (pre-declared
        expectation matching, `reconciled=True`), and the read-only ORM
        one-liners each field is sourced from — explicitly no new logging
        and no new `ops/` module.
      - **Disposition (D9):** scoped, identity-verified delete order
        (`BulkSendRecipient` → `BulkSend` matched by `client_request_id`
        **and** `scheduled_by_id` → `recipients_file` media artifact), then
        re-verify `(jobs, v2, ledger) == (0, 0, 0)`.
      - **The `MAX_ROWS=20` binding constraint (D10):** state verbatim that
        no engineer may change `BULK_PROCESSING_V2_CANARY_MAX_ROWS` or the
        `max_rows != 20` check in `ops/td02c_settings_gate.py` for the
        duration of Stage 1, and why (unconditional check reached by every
        `ops/` deployment preflight).
      Every step in this section that constitutes production execution
      (selecting users, editing `.env`, restarting the service, running the
      gate against production, capturing production evidence, disposing of
      rows, declaring PASS/abort) is written as **prose for a future
      authorized operator to follow** — see Section (B) below. Writing this
      documentation is not running it.

- [ ] **6. `.env.example` — investigated, not applicable**
      Verified via repository search: no `.env.example` file exists in this
      repository (glob for `.env.example` at repo root returns no match).
      Design's condition ("only if it already documents these flags") is
      therefore not met. No file is created or modified by this task; this
      entry records that the check was performed, per the design's category
      (A) table, rather than silently skipping it.

### Phase 3 — Final consistency check (sequential, after Phase 0–2)

- [x] **7. Run the affected test suites**
      `python -m unittest ops.tests.test_td02c_settings_gate -v` → all
      green, including the new N-entry cases.
      `python manage.py test relay.tests.test_bulk_v2_canary -v 2` → all
      green, including the new multi-entry confirmation cases.
      `python -m unittest discover -s ops/tests -p "test_*.py" -v` → no
      regression in any other `ops/tests/test_*.py` module; record the pass
      count against `MINIMUM_OPS_PASSED` in
      `ops/deployment_test_profile.py` (do not raise the floor as part of
      this change unless the count increase is itself under review in this
      same commit, per existing documented policy).

- [x] **8. Static checks**
      `python -m py_compile ops/td02c_settings_gate.py`
      `git diff --check`

- [x] **9. Confirm scope containment (design §13's "not produced" list)**
      `git diff --stat -- relay/api.py relay/services/bulk_v2_canary.py relay/services/bulk_import.py config/settings.py ops/bulk_v2_canary_client.py`
      must return empty.
      Additionally confirm the `max_rows != 20` check in
      `ops/td02c_settings_gate.py` (currently line 67) is byte-identical
      before and after Task 2 — the arity extension must not touch it
      (design D10's binding constraint; this is a narrower, targeted check
      within a file that *is* otherwise modified, not a whole-file diff
      exclusion).

## (B) Documented for future authorized execution — NOT part of this apply

These are the production-execution steps that Task 5's runbook documents in
prose. None of them is a task in Section (A). None of them may be performed
by `sdd-apply`, by this SDD change, or as a side effect of checking off any
box above. Each requires separate, explicit owner authorization outside this
change (per proposal "Dependencies" and design §13):

- selecting and naming the 3–5 pilot users, and obtaining their consent;
- generating and distributing request-id tokens;
- editing the production `.env` (any of the four canary flags);
- restarting `django.service`;
- running `evaluate_django_settings` (with the new sequence arguments)
  against production settings;
- pilot users performing imports through the browser;
- reading production data to compile an evidence record;
- executing the disposition deletes;
- declaring Stage 1 PASS or abort.

## Explicitly out of scope for this tasks.md

Everything design §14 places out of scope for the whole SDD change: V2 send
capability, any send-worker change, V2 as default engine, V1 deprecation,
`MAX_ROWS` parametrization in `ops/td02c_settings_gate.py` (registered as a
named Stage 2 prerequisite only), full row-level V1/V2 equivalence
comparison, tightening `capabilities.bulk_processing_v2_create` to consult
the allowlist, and adding `valid=`/`invalid=` fields to the `bulk_v2_import`
log line.
