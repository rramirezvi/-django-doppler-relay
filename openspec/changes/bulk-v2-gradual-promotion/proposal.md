# Proposal: Bulk V2 Gradual Promotion — Stage 1 Internal Pilot

## Intent

Two production canary cycles closed clean (`ready_with_errors` on a malformed CSV, `ready` on a valid one) using a single dedicated technical account. That proves the mechanism, not the engine: V2 has never been exercised by real operators doing real work. Today promotion beyond one synthetic user is undefined — no cohort model, no per-execution evidence, no objective abort line. Stage 1 closes that gap: a controlled import-only pilot for 3–5 real internal staff, `MAX_ROWS` frozen at 20.

## Scope

### In Scope
- Cohort model: 3–5 real staff onboarded via the existing multi-value allowlist (`relay/services/bulk_v2_canary.py::normalize_allowlist`, `config/settings.py:161-165`), permissioned through the existing `Operadores UI` group (`relay/services/operator_permissions.py`).
- Objective entry / promote / abort criteria, reusing the proven canary signals: `firewall_breach`, jobs delta, `EmailMessage` delta, scoped disposition.
- Per-execution evidence from existing structured logs (`bulk_v2_canary decision=…`, `bulk_v2_import … result=…`, both `relay/api.py`) and V1/V2 parity observables.
- Flags-only activate/deactivate runbook (`.env` + `django.service` restart), already validated twice.
- Directed disposition of pilot rows: `BulkSendRecipient` → `BulkSend` → `recipients_file`, scoped and identity-verified.

### Out of Scope (each requires its own SDD initiative)
- **V2 send capability.** V2 is structurally import-only: `relay/api.py:548-573` (V2 branch only calls `BulkImportService(...).import_file()`; enqueue sits in a mutually-exclusive `elif`), `relay/services/bulk_processing.py:39-42` (raises `ValueError` for non-legacy), `relay/management/commands/process_bulk_scheduled.py:18-22,40-44` (filters `ENGINE_LEGACY`). Not decomposed here, not a hidden task.
- Any send-worker change; V2 scheduled sends; V2 as default engine; V1 deprecation.
- **Stage 2 prerequisite (future):** `ops/td02c_settings_gate.py:28-77` enforces `max_rows != 20 → max_rows_mismatch` independent of `expect_active`, and `ops/td02c_deployment_runner.py:241-257` runs it (`expect_active=False`) before every deployment. Raising the cap without first parametrizing that check would fail-close all future `ops/` deploys. Stage 1 keeps 20, so an active pilot cannot break deployments.
- Activating flags, running imports, or any production access during this SDD change.

## Capabilities

### New Capabilities
- `bulk-v2-pilot-cohort`: cohort selection, allowlist control, entry/promote/abort criteria, disposition.
- `bulk-v2-pilot-evidence`: per-execution evidence set and V1/V2 parity judgment from existing logs.

### Modified Capabilities
- None. `bulk-v2-canary-activation` / `bulk-v2-canary-execution` remain change-scoped deltas (no `openspec/specs/` source of truth yet).

## Approach

Reuse, don't rebuild. The allowlist is already multi-value, so **Stage 1 likely needs zero changes to `relay/` or `config/`**; candidate work is documentation plus optional reuse of `ops/bulk_v2_canary_client.py` for evidence capture. Design confirms or refutes this.

### (A) Support code vs (B) production execution — binding separation
- **(A)** = repo artifacts only: runbook, criteria, optional `ops/` evidence tooling. This is all `sdd-apply` may do.
- **(B)** = activating flags and running the pilot in production. **Completing (A) is never authorization for (B).** (B) requires separate explicit owner authorization outside this change. `sdd-design` and `sdd-tasks` MUST preserve this split; no task may bundle (B) into apply.

## Affected Areas

| Area | Impact | Description |
|---|---|---|
| `openspec/changes/bulk-v2-gradual-promotion/` | New | Proposal, specs, design, tasks |
| `ops/README.md` | Modified | Stage 1 cohort activate/deactivate/abort runbook |
| `ops/` tooling | Possibly new | Evidence capture, only if design proves it needed |
| `relay/`, `config/`, `ops/td02c_settings_gate.py` | Unchanged | No behavior change; `MAX_ROWS` stays 20 |

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| (A) apply misread as authorization to run the pilot | Med | Explicit A/B split above; carried into design and tasks |
| Real users hit V2 gaps the synthetic canary never touched | Med | Import-only; abort criteria; V1 stays default |
| Pilot rows pollute production | Med | Directed disposition, `max_rows=20`, identity-verified deletes |
| Scope creep into send capability | Med | Named non-goal requiring a separate initiative |
| Cohort too small to judge parity | Low | Evidence set defined before activation |

## Rollback Plan

Flags first: clear `BULK_PROCESSING_V2_CANARY_REQUEST_IDS` / `_USER_IDS`, set the enable flag off, restart `django.service`, re-run the settings gate with `expect_active=False`. Data second: directed disposition of pilot rows. Code third: none expected — if (A) touched `ops/`, revert the commit. V1 is unaffected throughout (`relay/models.py:206-216` blocks `engine_version` mutation after import starts).

## Dependencies

- Owner authorization + identity of the 3–5 pilot users (not yet issued).
- Pilot users already `is_active`, `is_staff`, and members of `Operadores UI`.
- Production at the confirmed baseline before any (B) activation.

## Success Criteria

- [ ] Cohort model, entry/promote/abort criteria, and evidence set documented and objective.
- [ ] Runbook covers activate, deactivate, abort, and disposition — flags-only.
- [ ] Whether Stage 1 requires any code change is answered explicitly (yes/no, with evidence).
- [ ] Stage 2 `max_rows` parametrization registered as a future initiative, not a task here.
- [ ] No flags activated, no imports run, no production access in this change.

## Open Questions

1. Who selects the 3–5 pilot users, and do we allowlist by user id, request id, or both?
2. Minimum pilot volume/duration before Stage 1 is declared PASS?
3. Retain pilot rows as evidence for a fixed window, or dispose immediately per cycle?
4. Is any V1/V2 parity signal missing from today's logs, or is existing logging sufficient?
