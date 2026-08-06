# Tasks: Activate Bulk Processing Engine V2 Canary

Scope reminder: every task below produces reviewable code or docs only. No
task flips a flag, restarts a service, or touches production. `relay/` and
`config/settings.py` are not modified by any task (design's explicit
"Unchanged" affected area) — Task 12 verifies that boundary held.

Test convention followed (matches existing pairing in `ops/tests/test_td02c_*.py`):
one `unittest.TestCase`-based test module per implementation module, written
and run **before** the implementation it covers, under the isolated
PostgreSQL profile (`ops.deployment_test_profile`) — never the `relay/`
Django test DB. `relay/tests/test_bulk_v2_*.py` is not touched by this change
since no `relay/` behavior changes.

## Phase 0 — Parameterize existing modules (prerequisite for the client)

These two pairs touch different files and have no dependency on each other;
they can be done in parallel. Both must land before Phase 1, because the new
client wires one `CanaryProfile` into both.

- [ ] **1. [Test-first, parallel with 3] Extend `ops/tests/test_td02c_settings_gate.py`**
      Add cases asserting `evaluate_effective_settings` accepts new
      keyword-only `expected_request_id` / `expected_user_id`, defaulting to
      the current `CANARY_REQUEST_ID` / `CANARY_USER_ID` constants (existing
      call sites and assertions must keep passing unmodified), and that
      supplying an override changes the expected match. Covers design D3.
      Run: `python -m unittest ops.tests.test_td02c_settings_gate -v` → RED
      (new cases fail, module not yet changed).

- [ ] **2. [Impl, after 1] Modify `ops/td02c_settings_gate.py`**
      Add keyword-only `expected_request_id: str = CANARY_REQUEST_ID` and
      `expected_user_id: int = CANARY_USER_ID` to `evaluate_effective_settings`
      (threaded through by `evaluate_django_settings`, unchanged signature).
      Run: `python -m unittest ops.tests.test_td02c_settings_gate -v` → GREEN,
      all prior tests unchanged.

- [ ] **3. [Test-first, parallel with 1] Extend `ops/tests/test_td02c_http_client.py`**
      Update the two existing `write_post_curl_config` call sites (currently
      relying on hardcoded TD-02C literals at lines 407–413) to pass explicit
      `client_request_id` / `template_id` / `template_name` / `subject`
      keyword arguments, and assert the emitted `post.curl.conf` contains the
      supplied values, is mode `0600`, and no secret appears in
      `safe_curl_argv`. Covers design D2.
      Run: `python -m unittest ops.tests.test_td02c_http_client -v` → RED.

- [ ] **4. [Impl, after 3] Modify `ops/td02c_http_client.py`**
      `write_post_curl_config` takes `client_request_id`, `template_id`,
      `template_name`, `subject` as required keyword arguments, replacing the
      hardcoded literals; `send_now=False` / empty `scheduled_at` stay
      hardcoded (import-only invariant, not caller-configurable).
      Run: `python -m unittest ops.tests.test_td02c_http_client -v` → GREEN.

## Phase 1 — Canary execution client (depends on Task 2 and Task 4)

- [ ] **5. [Test-first] Write `ops/tests/test_bulk_v2_canary_client.py`**
      One test module, all scenarios RED before Task 6. Itemized by spec
      requirement (`bulk-v2-canary-execution` unless noted):
      - Gate-Guarded Execution Refusal: gate pass → proceeds to build/send;
        gate fail → refuses, reports gate reasons, **zero network calls**,
        no workspace/credential touched.
      - Credential-Free Execution: valid env-var or mode-0600 file source →
        no secret in argv, no secret in any log line; missing credential
        source → refusal, reported, before any HTTP attempt.
      - Client-Layer Import-Only Enforcement: payload always has
        `send_now=False` and empty `scheduled_at`; no client option exists to
        override either; attempted `sender_id` or extra field →
        `import_only_violation` raised before workspace/credential file is
        touched (design D4).
      - Config-bytes (extends the Task 3 pattern): exact 9 allowed fields,
        `engine_version=v2`, file mode `0600`, no secret in argv.
      - Profile/gate/setting consistency (design D3): `CanaryProfile` value
        must equal both the gate's `expected_request_id`/`expected_user_id`
        and the raw effective Django setting before any HTTP; mismatch →
        `profile_mismatch`, no HTTP attempted.
      - Expected-delta assertion (design D6): accepts
        `bulk_sends +1, bulk_sends_v2 +1, recipients +N, jobs +0, messages +0`
        via injected `Baseline` doubles; any nonzero `jobs`/`messages` delta →
        `firewall_breach`, treated as abort-and-escalate, not a normal error.
      - Fingerprint-Only Logging: emitted JSONL line contains the
        `sha256(client_request_id)[:12]` fingerprint, never the raw
        `client_request_id`, raw user id, or recipient content.
      - Testable in Isolated Profile: whole module runs under
        `ops.deployment_test_profile` isolation, no network, no production
        credentials — assert via existing profile helpers, matching the
        pattern in `ops/tests/test_td02c_authenticated_get_runner.py`.
      Run: `python -m unittest ops.tests.test_bulk_v2_canary_client -v` → RED
      (module does not exist yet).

- [ ] **6. [Impl, after 5] Create `ops/bulk_v2_canary_client.py`**
      Composition layer only (design's stated non-negotiable): reuse
      `CurlOperations`, `DjangoState`, `validate_credential_file`,
      `delete_exact_file`, `validate_module_entrypoint` from
      `ops/td02c_authenticated_get_runner.py`; reuse `write_post_curl_config`,
      `safe_curl_argv`, `classify_canary_response`, `secure_cookie_workspace`
      from `ops/td02c_http_client.py` (now parameterized by Task 4). Add:
      `CanaryProfile` frozen dataclass, `run_canary(profile, *, csv_path,
      credential_file, service_unit) -> int`, the `ERROR_CODES` set from the
      design's Interfaces/Contracts block, gate precondition (D5, runs before
      any workspace/credential/TLS/HTTP), import-only allowlist check (D4),
      profile/gate/setting consistency check (D3), expected-delta assertion
      (D6), and evidence emission via the existing `SafeDiagnosticLog` /
      `StageDiagnostic` / `emit_counts` pattern. No new HTTP stack, no new
      secret-handling code path.
      Run and iterate to green:
      `python -m unittest ops.tests.test_bulk_v2_canary_client -v`
      Regression check (Phase 0 not broken by Phase 1):
      `python -m unittest ops.tests.test_td02c_settings_gate ops.tests.test_td02c_http_client -v`

## Phase 2 — Flags-only runbook and evidence/disposition docs (`ops/README.md`)

Can be drafted in parallel with Phase 1 (design and specs are already final),
but finalize wording against the Task 6 interface (`ERROR_CODES`, evidence
field names) before marking done. All three land in `ops/README.md`; per
`work-unit-commits`, they are one work unit / one commit (PR1) since none is
independently deployable without the others — same doc, same reviewable
runbook story.

- [ ] **7. Doc: Production preflight + flags-only activation/rollback**
      Requirements covered: *Production Preflight Before Flag Changes*,
      *Flags-Only Activation*, *Flags-Only Deactivation and Rollback*,
      *Gate-Verified Activation State*. Content: fresh read-only preflight
      re-confirming the production commit immediately before any flag change
      (never trust the earlier TD-02C confirmation alone); the four flag
      names (`BULK_PROCESSING_ENGINE_V2`, `BULK_PROCESSING_V2_CANARY_ENABLED`,
      `BULK_PROCESSING_V2_CANARY_REQUEST_IDS`,
      `BULK_PROCESSING_V2_CANARY_USER_IDS`) plus Django restart, no code
      deploy; `max_rows` pinned at 20, external template lookup pinned
      `False`; both directions verified only via
      `td02c_settings_gate.evaluate_django_settings(expect_active=...)`,
      never a new parser. Mirror the design's Runbook (9-step) and
      Three-layer rollback tables; state the mandatory flags-first ordering
      (`_django_state()` hardcodes `expect_active=False`, so deployment
      preflight fails closed while canary flags are ON).

- [ ] **8. Doc: Promotion/abort criteria + evidence capture checklist**
      Requirements covered: *Promotion and Abort Criteria*, *Evidence
      Capture*. Content: promotable only when gate reports active, client
      POST succeeds, and evidence was captured; abort immediately on any gate
      failure or client refusal, no retry until the reason is addressed.
      Evidence checklist: gate result code + reasons, fingerprinted client
      log line, confirmation of zero external calls, confirmation
      `EmailMessage.objects.count() == 0` for the run — explicitly never raw
      `client_request_id`, raw user id, or recipient data.

- [ ] **9. Doc: Disposition of canary-created rows**
      Requirement covered: *Disposition of Canary-Created Rows*. Content:
      why retention is not viable (`_validate_operational_gates` requires
      `BulkSend.objects.filter(engine_version="v2").count() == 0` and
      `BulkSendRecipient.objects.count() == 0`, blocking all future TD-02C
      deployment otherwise); delete `BulkSend` + `BulkSendRecipient` rows and
      the `recipients_file` media artifact under `bulk_recipients/` **after**
      evidence capture (sequenced after Task 8's checklist in the doc); verify
      a subsequent operational-gates check reports `(jobs, v2, ledger) ==
      (0, 0, 0)`. State explicitly: no `EmailMessage` rows exist to dispose of
      — one appearing would be a firewall breach, not a disposition item.

## Phase 3 — Final consistency check (sequential, after all of the above)

- [ ] **10. Run the full `ops/` isolated-profile suite**
      `python -m unittest discover -s ops/tests -p "test_*.py" -v`
      Confirm all green, including Phase 0/1 additions, with no regression in
      any other `ops/tests/test_*.py` module. Record the pass count against
      `MINIMUM_OPS_PASSED` / `MINIMUM_HTTP_CLIENT_PASSED` in
      `ops/deployment_test_profile.py`; per existing documented policy, only
      raise a floor once the real suite count has grown stably — do not raise
      it as part of this change unless the count increase is itself the
      change being reviewed in that commit.

- [ ] **11. Static checks per `ops/README.md`'s "Local validation" section**
      `python -m py_compile ops/bulk_v2_canary_client.py ops/td02c_http_client.py ops/td02c_settings_gate.py`
      `git diff --check`

- [ ] **12. Confirm scope containment**
      `git diff --stat -- relay/ config/settings.py` must return empty,
      matching the proposal's affected-areas table (`relay/`,
      `config/settings.py` — Unchanged) and design's explicit non-negotiable
      that `evaluate_canary`, the V1/V2 firewall, and `targeted_rollback()`
      stay untouched.

## Explicitly out of scope for this tasks.md

Phase 3 of the proposal (owner-authorized production execution: setting
flags, restarting Django, running the client against production, disposing
of rows) is a separate future authorization, not a task in this SDD change.
