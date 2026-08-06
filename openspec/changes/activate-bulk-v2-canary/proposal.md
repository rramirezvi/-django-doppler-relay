# Proposal: Activate Bulk Processing Engine V2 Canary

## Intent

V2 ships behind a 14-gate fail-closed canary but has never completed one successful production run. The single real attempt aborted pre-POST on a settings-gate parsing bug. The fix (`6fed6dd`) is code-present in the confirmed production checkout (`8212a4e` == `td02c-final`, TD-02C closure 2026-08-05) but has never been exercised by a live canary POST — code-verified, not behavior-verified. Execution also relied on untracked local scripts, so no run is auditable or repeatable. This change plans a controlled canary activation with first-class flag rollback and evidence.

## Scope

### In Scope
- Committed, credential-free canary execution client (`ops/bulk_v2_canary_client.py`) + tests: import-only POST, refuses to run unless `td02c_settings_gate` reports active.
- Flags-only activate / deactivate / rollback runbook, separate from code deployment.
- Promotion, abort, and evidence criteria.
- Disposition plan for rows created by the import-only run.

### Out of Scope
- Any production access, flag flip, credential creation, or push in this change.
- `send_now`, scheduled sends, external template lookup, async/worker V2 — permanently out.
- Raising `max_rows` above 20; V2 pipeline redesign; reopening TD-02C.

## Capabilities

### New Capabilities
- `bulk-v2-canary-activation`: preconditions, flag activation/rollback, and promotion criteria for the production canary.
- `bulk-v2-canary-execution`: committed, auditable import-only canary request client.

### Modified Capabilities
- None.

## Approach

Reuse, don't rebuild. `evaluate_canary`, the V1/V2 firewall, and `targeted_rollback()` stay untouched. Add only the two missing artifacts: an auditable execution client, and a flags-only runbook whose active/inactive states are proven by the existing settings gate (`expect_active` True/False). Fresh allowlist tokens replace the burned TD-02C values.

## Affected Areas

| Area | Impact | Description |
|---|---|---|
| `ops/bulk_v2_canary_client.py` | New | Gate-guarded import-only POST client; no stored credentials |
| `ops/tests/` | New | Client unit tests (isolated profile) |
| `ops/README.md` | Modified | Flags-only activate/rollback runbook |
| `relay/`, `config/settings.py` | Unchanged | No behavior change |

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Client leaks credentials or raw ids | Med | Env-only creds, fingerprint logging; if it cannot be credential-free, split it into its own change |
| Fix `6fed6dd` is code-present in the last confirmed production checkout (`8212a4e` == `td02c-final`) but was never behaviorally re-validated by a live canary POST after the 2026-08-01 abort | Med | Treat the first real attempt as unproven; a different failure mode may surface. Fresh read-only preflight, abort on any gate failure |
| Canary rows pollute production data | Med | Documented disposition; `max_rows=20` cap |
| Exceeds 400-line review budget | Med | Chain: PR1 runbook, PR2 client |

## Rollback Plan

Flags first: set the three canary vars to False/empty, restart Django, re-run the settings gate with `expect_active=False`. Code second: existing `targeted_rollback()`. Data third: dispose of canary `BulkSend`/`EmailMessage` rows per the documented plan. Each step independently verifiable.

## Dependencies

- Isolated PostgreSQL test profile for any `ops/` test run.
- Owner-issued production authorization and fresh allowlist tokens (not yet issued).

## Success Criteria

- [ ] Flags-only activation and rollback documented and gate-verified in both directions.
- [ ] Canary client committed, tested, credential-free, and refusing to POST when the gate is inactive.
- [ ] Promotion/abort criteria and evidence checklist explicit.
- [ ] No production access, flag flip, or send occurred in this change.

## Open Questions

1. Production checkout is confirmed at `8212a4e` as of TD-02C closure (2026-08-05). A fresh read-only preflight should still re-confirm it immediately before any Phase 3 execution, since time has passed and this change does not re-verify production state itself.
2. New canary request-id / user-id token values, and who issues them.
3. Retain canary rows as evidence, or delete after the run?
