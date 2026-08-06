# Design: Activate Bulk Processing Engine V2 Canary

## Technical Approach

`ops/bulk_v2_canary_client.py` is a **composition layer, not a new HTTP stack**. The
session machinery (`CurlOperations`, `DjangoState`, `validate_credential_file`,
`delete_exact_file`, `validate_module_entrypoint`) already exists in
`ops/td02c_authenticated_get_runner.py`; the POST artifacts
(`write_post_curl_config`, `safe_curl_argv`, `classify_canary_response`,
`secure_cookie_workspace`) already exist in `ops/td02c_http_client.py`. The new
module adds exactly three things the repo lacks: (1) a settings-gate precondition,
(2) the POST execution step the GET-only runner deliberately refuses, (3) an
expected-delta assertion. `relay/`, `config/settings.py` and `evaluate_canary`
stay untouched. The runbook is documentation plus existing gates — no new tooling.

## Architecture Decisions

| # | Decision | Alternatives rejected | Rationale |
|---|---|---|---|
| D1 | Standalone `ops/` module run as `python -m ops.bulk_v2_canary_client` | A `manage.py` command | Verified, not assumed: `ops/README.md` never claims "privilege separation". The real invariants it states are (a) ops flows run *around* the Django lifecycle — pre/post restart, rollback, and bootstraps where the module is absent from deployed HEAD; (b) `no source .env`; (c) execution as the discovered systemd service user with `validate_module_entrypoint` fail-closed checks; (d) direct `python ops/*.py` is unsupported. A `manage.py` command inherits none of those and would couple the canary to a bootable Django process. |
| D2 | Parameterize the existing `write_post_curl_config` with a `CanaryProfile` | New config writer inside the client | `write_post_curl_config` already emits the exact 9-field multipart body to `/api/bulk-sends/`, 0600, secrets out of argv. Duplicating it would create a second secret-bearing surface. Today its `client_request_id`/`template_*` values are hardcoded to the burned TD-02C literals (lines 407-413) — they become required keyword arguments. |
| D3 | One profile feeds both the gate and the POST | Independent constants in gate and client | `td02c_settings_gate.CANARY_REQUEST_ID` and the curl config would silently diverge on token rotation. Add keyword-only `expected_request_id`/`expected_user_id` to `evaluate_effective_settings` (defaults = current constants, so existing tests and `td02c_deployment_runner` are unchanged). The client asserts profile == gate expectation == effective raw setting before any HTTP. |
| D4 | Client-side import-only allowlist (defense in depth) | Trust server `evaluate_canary` | The server gate stays authoritative, but the client refuses to *construct* a forbidden body: exactly the 9 allowed fields, `engine_version=v2`, `send_now=False`, empty `scheduled_at`, no `sender_id`, no external-template variable. Violation raises `import_only_violation` before the workspace or credential file is touched. |
| D5 | Gate is a precondition, not a post-check | Run POST, inspect result | `evaluate_django_settings(settings, expect_active=True)` runs in-process first. On failure: exit non-zero with `settings_gate_failed`, no workspace, no credential read, no TLS, no HTTP. |
| D6 | Expected-delta assertion replaces `assert_functional_unchanged` | Reuse the GET runner's "nothing changed" rule | The canary intentionally creates rows, so the GET rule cannot apply. Required deltas: `bulk_sends +1`, `bulk_sends_v2 +1`, `recipients +N`, **`jobs +0`, `messages +0`**. Verified in `relay/services/bulk_import.py`: import-only creates only `BulkSendRecipient`. A non-zero `messages`/`jobs` delta is a V1/V2 firewall breach → abort and escalate. |
| D7 | Canary rows are **deleted**, not retained | Keep rows as evidence | Decisive constraint: `td02c_deployment_runner._validate_operational_gates` requires `(jobs, v2, ledger) == (0, 0, 0)` in both `preflight-only` and post-merge. Retained rows permanently block every future TD-02C deployment. (`--mode rollback` is unaffected.) Disposition must also delete the media artifact, because `BulkSend.recipients_file` is a `FileField(upload_to="bulk_recipients/")`. Answers proposal Open Question 3. |

## Data Flow

```
CanaryProfile ─┬─→ evaluate_effective_settings(expect_active=True)   [precondition]
               ├─→ import-only allowlist check                      [client-side]
               └─→ write_post_curl_config(...) ──→ safe_curl_argv ──→ curl
                                                                       │
 credential file (0600, outside checkout) ─→ CurlOperations.authenticate
                                                                       ▼
 DjangoState.baseline() ──────────────────────→  POST /api/bulk-sends/ ──→ 201/json
        │                                                              │
        └──→ expected-delta assertion ←── DjangoState.baseline() ←─────┘
                              │
                              ▼
              JSONL evidence (0600, outside checkout)
```

## Runbook: flags-only activation (each step gated)

| # | Step | Verification |
|---|---|---|
| 1 | Read effective state | `evaluate_django_settings(expect_active=False)` PASS; `.env` inspected, never sourced |
| 2 | Propose canonical raw values | Exactly `<fresh-request-id>` and `<user-id>`; no spaces, duplicates, wildcards |
| 3 | Apply `.env` edit (operator) | Record prior line values for reversal |
| 4 | Restart `django.service` | `td02c_worker_gate` pre/post + `validate_readiness_layers` |
| 5 | Prove ON | `evaluate_django_settings(expect_active=True)` PASS |
| 6 | Execute client | `201 application/json`, `classify_canary_response` allowed |
| 7 | Verify results | Expected-delta assertion; `bulk_v2_canary decision=canary_allowed` in app log |
| 8 | Deactivate (`.env` → `False`/empty) + restart | `evaluate_django_settings(expect_active=False)` PASS |
| 9 | Dispose data | Counts back to `(0, 0, 0)`; media file removed |

Abort at any failed step; steps 8–9 always run, success or failure.

## Three-layer rollback

| Layer | Trigger | Mechanism | Verification |
|---|---|---|---|
| Flags | Any abort after step 3, or normal completion | `.env` revert + restart | Gate `expect_active=False` |
| Code | Only if a deployment/fast-forward is implicated | Existing `targeted_rollback()` via `--mode rollback` | Existing runner evidence |
| Data | Whenever any canary row exists | Delete `BulkSendRecipient` → `BulkSend` → media file, scoped to the canary `client_request_id` | `(jobs, v2, ledger) == (0, 0, 0)` |

**Ordering is mandatory, flags first**: `_django_state()` hardcodes
`expect_active=False`, so any `preflight-only`/`deploy-only` fails closed while the
canary flags are ON. Data last, because it restores deployability.

## Evidence and logging

One sanitized JSON line per substage via the existing `SafeDiagnosticLog` /
`StageDiagnostic`, plus `emit_counts` for `baseline_before`/`baseline_after`.
Identifiers follow `relay/api.py:447-449` — `sha256(client_request_id)[:12]`
fingerprint, never the raw token. Recorded: substage, PASS/FAIL, exit code,
duration, classification, and for HTTP only method, path, status, content type,
sanitized Location, redirect count, `ssl_verify_result`, total time. Never
recorded: bodies, cookies, CSRF, credentials, recipient data, raw ids, `nginx -T`
dumps. Evidence file is absolute, outside the checkout, dir `0700` / file `0600`,
published by atomic rename and re-read as JSON before PASS.

## File Changes

| File | Action | Description |
|---|---|---|
| `ops/bulk_v2_canary_client.py` | Create | Gate-guarded import-only POST client (PR2) |
| `ops/td02c_http_client.py` | Modify | `write_post_curl_config` takes `CanaryProfile`; burned literals become arguments |
| `ops/td02c_settings_gate.py` | Modify | Keyword-only `expected_request_id`/`expected_user_id` (defaults preserve behavior) |
| `ops/tests/test_bulk_v2_canary_client.py` | Create | Unit tests, isolated profile |
| `ops/README.md` | Modify | Flags-only runbook, rollback, disposition (PR1) |
| `relay/`, `config/settings.py` | Unchanged | No behavior change |

## Interfaces / Contracts

```python
@dataclass(frozen=True)
class CanaryProfile:
    request_id: str          # fresh token; must equal the effective raw setting
    user_id: int
    template_id: str
    template_name: str
    subject: str
    max_rows: int = 20

def run_canary(profile: CanaryProfile, *, csv_path: Path,
               credential_file: Path, service_unit: str) -> int: ...

ERROR_CODES = {
    "settings_gate_failed", "profile_mismatch", "import_only_violation",
    "unexpected_row_delta", "firewall_breach", "disposition_required",
    "client_module_path_mismatch", "client_module_not_importable_from_checkout",
    "client_entrypoint_identity_mismatch",
    # Review-driven follow-up fixes (post-PR3): csv_path is validated with
    # the same import_only_violation code as the other four profile fields;
    # any unexpected exception in run_canary/main is classified
    # "unexpected_error" and never propagates; lock contention on the
    # reused ops.td02c_deployment_runner.repository_lock is translated to
    # "concurrent_execution_blocked".
    "unexpected_error", "concurrent_execution_blocked",
}  # plus the existing td02c_http_client taxonomy
```

## Testing Strategy

| Layer | What to test | Approach |
|---|---|---|
| Unit | Gate refusal before any side effect; profile/gate/setting mismatch; import-only allowlist rejects `send_now`, `scheduled_at`, `sender_id`, extra fields | Pure functions + fake operations, no network |
| Unit | Config bytes: exact 9 fields, `0600`, no secrets in argv | Assert on generated `post.curl.conf` (extends `test_td02c_http_client.py`) |
| Unit | Delta assertion: accepts `+1/+1/+N/0/0`, rejects any `messages`/`jobs` delta | Injected `Baseline` doubles |
| Unit | Evidence redaction: fingerprint only, no raw token in any emitted line | Capture the JSONL stream |
| Integration | Isolated PostgreSQL profile only | `ops.deployment_test_profile` `isolated`; never production |
| E2E | Not automated | The production run *is* the E2E, executed only under owner authorization |

## Threat Matrix

| Boundary | Applicability | Design response | Planned RED tests |
|---|---|---|---|
| Documentation-like paths | N/A — no file classification or execution of repo content | — | — |
| Git repository selection | N/A — the client performs no Git operation | — | — |
| Commit state | N/A | — | — |
| Push state | N/A | — | — |
| PR commands | N/A | — | — |

Subprocess boundary is applicable and is handled by the existing rules rather than
a new mechanism: `shell=False`, fixed argv via `safe_curl_argv`, secrets only in a
mode-0600 config, no `-k`, no `--location`, bounded timeouts, workspace deleted in
`finally`. Tests above cover the argv/config cases.

## Migration / Rollout

No migration. Chained delivery to stay under 400 lines: **PR1** = runbook +
disposition docs (`ops/README.md`); **PR2** = client + tests + the two small
parameterizations. Both are independently reviewable and revertible. No production
access, flag flip, credential creation, or push occurs in this change.

## Spec cross-check (delta specs read at authoring time)

Three points where the delta specs and this design must be reconciled before tasks:

| Spec statement | Design position | Resolution |
|---|---|---|
| Execution spec: payload MUST include `import_only=True` | `_bulk_send_create` has no `import_only` POST field — the server hardcodes `import_only=True` when calling `evaluate_canary` (`relay/api.py:445`) | Client enforces import-only by *field exclusion*, not by an unrecognized extra field: `send_now=False`, empty `scheduled_at`, no `sender_id`, engine_version `v2`. Spec wording should say "import-only semantics", not a literal field. |
| Activation spec: disposition is "retain as evidence, **or** delete" | D7 — retain is not viable | The `(jobs, v2, ledger) == (0, 0, 0)` gate in `_validate_operational_gates` makes retention block all future deployments. Delete is the only option; evidence is the JSONL record, not the rows. |
| Activation spec: disposition covers `BulkSend`/`EmailMessage` | D6/D7 | Import-only creates `BulkSend` + `BulkSendRecipient` + a `bulk_recipients/` media file, and **zero** `EmailMessage`. An `EmailMessage` row is a firewall breach, not a disposition item. |

## Open Questions

- [ ] Who issues the fresh request-id / user-id, and is the value committed as a gate
      default or supplied per run? (Design supports both; default keeps old constants.)
- [ ] Re-confirm the production checkout read-only immediately before Phase 3; this
      change does not verify production state.
- [ ] Confirm the operator-approved absolute path for evidence and credential files.
