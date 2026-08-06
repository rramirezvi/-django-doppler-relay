# Bulk V2 Canary Activation Specification

## Purpose

Defines the flags-only runbook to activate, gate-verify, and roll back the
production Bulk Processing Engine V2 canary, plus its promotion/abort
criteria, evidence capture, and canary-row disposition. Reuses
`evaluate_canary`, `targeted_rollback()`, and `td02c_settings_gate` unchanged.

## Requirements

### Requirement: Production Preflight Before Flag Changes

The runbook MUST run a fresh, read-only preflight confirming the production
checkout commit immediately before any flag change, even when a prior commit
(8212a4e / td02c-final) was already confirmed at an earlier date.

#### Scenario: Preflight confirms matching commit

- GIVEN the operator is about to begin flag activation
- WHEN the read-only preflight check runs against production
- THEN the checked-out commit MUST equal the previously confirmed commit
- AND activation proceeds only on a match

#### Scenario: Preflight detects drift and aborts

- GIVEN the preflight check runs against production
- WHEN the checked-out commit does not match the confirmed commit
- THEN the runbook MUST abort and MUST NOT change any flag

### Requirement: Flags-Only Activation

Activation MUST be performed only via `BULK_PROCESSING_ENGINE_V2`,
`BULK_PROCESSING_V2_CANARY_ENABLED`, `BULK_PROCESSING_V2_CANARY_REQUEST_IDS`,
`BULK_PROCESSING_V2_CANARY_USER_IDS`, and a Django restart — no code deploy.
`BULK_PROCESSING_V2_CANARY_MAX_ROWS` MUST stay 20 and
`BULK_PROCESSING_V2_ALLOW_EXTERNAL_TEMPLATE_LOOKUP` MUST stay `False`.

#### Scenario: Operator activates via flags only

- GIVEN preflight passed and fresh allowlist tokens were issued
- WHEN the four canary flags are set and Django restarts
- THEN no application code is deployed or modified
- AND `max_rows` remains 20 and external lookup remains `False`

### Requirement: Flags-Only Deactivation and Rollback

Rollback MUST revert the canary flags to `False`/empty and restart Django,
independent of any code-level rollback step.

#### Scenario: Operator rolls back via flags only

- GIVEN the canary is currently active
- WHEN the engine, canary-enabled, and allowlist flags are cleared and Django restarts
- THEN the effective settings match the inactive state
- AND no code rollback is required to reach it

### Requirement: Gate-Verified Activation State

Both activation and rollback MUST be verified with the existing
`td02c_settings_gate.evaluate_django_settings` (or
`evaluate_effective_settings`), called with `expect_active` matching intent —
never a new or duplicated parser.

#### Scenario: Gate confirms active state

- GIVEN flags were set per activation
- WHEN `evaluate_django_settings(settings, expect_active=True)` runs
- THEN the result MUST have `allowed=True`, `code="canary_settings_active"`

#### Scenario: Gate confirms inactive state after rollback

- GIVEN flags were reverted per rollback
- WHEN `evaluate_django_settings(settings, expect_active=False)` runs
- THEN the result MUST have `allowed=True`, `code="canary_settings_inactive"`

### Requirement: Promotion and Abort Criteria

The runbook MUST define explicit promotion and abort criteria.

#### Scenario: Promotion criteria met

- GIVEN the gate reports active, the client's import-only POST succeeds, and evidence was captured
- WHEN the operator evaluates the run
- THEN it MUST be marked promotable

#### Scenario: Abort criteria triggered

- GIVEN the gate reports a failure reason or the client refuses to run
- WHEN the operator evaluates the run
- THEN the runbook MUST require immediate abort and flags-only rollback, with no retry until the reason is addressed

### Requirement: Evidence Capture

Each activation/deactivation attempt MUST have evidence captured: gate result
code and reasons, the fingerprinted client log line, confirmation of zero
external calls, and confirmation that the import-only run created zero
`EmailMessage` rows — never raw ids or recipient data.

#### Scenario: Evidence recorded for a run

- GIVEN activation, gate verification, and a client run completed
- WHEN the operator compiles the evidence checklist
- THEN it MUST include the gate code, request fingerprint, and row count
- AND MUST NOT include raw `client_request_id`, raw user id, or recipient data

#### Scenario: Zero EmailMessage rows confirmed

- GIVEN the import-only canary run has completed
- WHEN the operator checks `EmailMessage.objects.count()` for the run
- THEN the count MUST be zero, confirming no send occurred

### Requirement: Disposition of Canary-Created Rows

`ops/td02c_deployment_runner.py::_validate_operational_gates` blocks any
future TD-02C-style deployment unless `BulkSend.objects.filter(engine_version
="v2").count() == 0` and `BulkSendRecipient.objects.count() == 0`. Retaining
canary rows would therefore permanently block deployment — deletion is the
only viable disposition. The runbook MUST delete all `BulkSend` and
`BulkSendRecipient` rows created by the import-only run, and MUST delete the
associated `recipients_file` media artifact (`BulkSend.recipients_file`,
stored under `bulk_recipients/`), after evidence has been captured. No
`EmailMessage` rows exist to dispose of (see Evidence Capture).

#### Scenario: Canary rows and file deleted after evidence capture

- GIVEN a canary run created `BulkSend`/`BulkSendRecipient` rows and left a `recipients_file` artifact under `bulk_recipients/`
- WHEN the operator closes out the run after capturing evidence
- THEN the runbook MUST delete those rows and the `recipients_file` artifact
- AND MUST verify a subsequent operational-gates check reports `(jobs, v2, ledger) == (0, 0, 0)`
