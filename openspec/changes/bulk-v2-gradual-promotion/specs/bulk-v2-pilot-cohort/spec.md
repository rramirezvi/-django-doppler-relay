# Bulk V2 Pilot Cohort Specification

## Purpose

Defines Stage 1 of Bulk Processing Engine V2 gradual promotion: a controlled,
import-only pilot for 3-5 real internal staff. Covers cohort selection,
allowlist control, entry/promote/abort criteria, and directed disposition of
pilot rows. Reuses the existing multi-value allowlist
(`relay/services/bulk_v2_canary.py::normalize_allowlist`,
`config/settings.py:161-166`) and the existing `Operadores UI` permission
group (`relay/services/operator_permissions.py`) unchanged. `MAX_ROWS` MUST
stay 20 for this capability's entire scope; no requirement or scenario in this
spec raises it. This capability is import-only; it MUST NOT enable or imply
V2 send capability, which is out of scope and reserved for a separate future
initiative.

## Requirements

### Requirement: Cohort Selection By User Id

The pilot cohort MUST consist of 3-5 real internal staff selected by the
product/ops owner and identified by Django user id
(`BULK_PROCESSING_V2_CANARY_USER_IDS`), with `client_request_id`
(`BULK_PROCESSING_V2_CANARY_REQUEST_IDS`) available as a narrowing filter on
top of the user allowlist, not as the primary selection axis. Every selected
user MUST already be `is_active`, `is_staff`, and a member of the
`Operadores UI` permission group before being added to the allowlist.

#### Scenario: Owner selects a compliant cohort

- GIVEN the product/ops owner identifies 3-5 candidate staff by user id
- WHEN each candidate is checked against `is_active`, `is_staff`, and
  `Operadores UI` membership
- THEN only candidates satisfying all three MUST be added to
  `BULK_PROCESSING_V2_CANARY_USER_IDS`
- AND a candidate failing any check MUST be excluded from the cohort

#### Scenario: Request id narrows, never substitutes for user id

- GIVEN a cohort user id is present in `BULK_PROCESSING_V2_CANARY_USER_IDS`
- WHEN that user submits a `client_request_id` not present in
  `BULK_PROCESSING_V2_CANARY_REQUEST_IDS`
- THEN `evaluate_canary` MUST return `request_not_allowed` and the import
  MUST NOT proceed, regardless of the user's allowlist membership

### Requirement: Allowlist Control Via Existing Multi-Value Mechanism

Cohort onboarding and removal MUST use the existing comma-separated,
multi-value allowlist parsing (`normalize_allowlist`) already present in
`relay/services/bulk_v2_canary.py` and `config/settings.py:161-166`, with no
code change required to support 3-5 concurrent entries.

#### Scenario: Multiple cohort members coexist in one allowlist value

- GIVEN `BULK_PROCESSING_V2_CANARY_USER_IDS` is set to a comma-separated list
  of 3-5 distinct positive integer user ids
- WHEN `normalize_allowlist` parses the value
- THEN it MUST return one normalized id per listed user with no duplicates
  and no error code
- AND no change to `relay/` or `config/` is required to support this count

#### Scenario: Malformed allowlist entry is rejected before any user is authorized

- GIVEN `BULK_PROCESSING_V2_CANARY_USER_IDS` or
  `BULK_PROCESSING_V2_CANARY_REQUEST_IDS` contains a duplicate, empty, glob
  character, or non-integer (for user ids) entry
- WHEN `normalize_allowlist` parses the value
- THEN it MUST return `canary_config_invalid` and the entire allowlist MUST be
  treated as empty, authorizing no one

### Requirement: MAX_ROWS Stays Fixed At 20

`BULK_PROCESSING_V2_CANARY_MAX_ROWS` MUST remain 20 for the full duration of
the Stage 1 pilot. No requirement, scenario, runbook step, or evidence
criterion in this capability may imply, request, or depend on a higher value.

#### Scenario: Pilot import at the row ceiling succeeds

- GIVEN a cohort user submits an allowlisted request with exactly 20 rows
- WHEN `evaluate_canary` evaluates `total_rows` against `row_limit`
- THEN the row-limit check MUST pass (`total_rows <= row_limit`) with
  `row_limit == 20`

#### Scenario: Pilot import above the row ceiling is refused

- GIVEN a cohort user submits an allowlisted request with 21 rows
- WHEN `evaluate_canary` evaluates the request
- THEN it MUST return `row_limit_exceeded` and the import MUST NOT proceed

### Requirement: Entry Criteria Before Stage 1 Activation

Before the runbook, when executed by an authorized operator, activates Stage
1 for any cohort member, all of the following MUST be true: the cohort member
satisfies user-id selection and permission checks; a minimum
volume/duration threshold for declaring Stage 1 PASS has been defined
(parameter left to the design phase or a later runbook, not hardcoded here);
and production is confirmed at the expected baseline commit via a fresh
read-only preflight, matching the pattern already proven for canary
activation.

#### Scenario: Entry blocked when the PASS threshold is undefined

- GIVEN a cohort has been selected and the allowlist is valid
- WHEN the operator prepares to activate Stage 1
- THEN activation MUST NOT proceed until a minimum volume/duration threshold
  for PASS has been explicitly defined and recorded

#### Scenario: Entry proceeds once all criteria are satisfied

- GIVEN the cohort, allowlist, PASS threshold, and preflight commit check all
  pass
- WHEN the runbook, executed by an authorized operator, activates the four
  canary flags for the pilot cohort
- THEN activation MUST occur via flags-only change and Django restart, with
  no code deploy, consistent with the existing activation runbook

### Requirement: Promotion Criteria Reuse Proven Canary Signals

Promotion of the pilot toward wider rollout MUST be judged using the same
signal set already proven across two production canary cycles: gate-verified
active state, zero unexpected jobs delta, zero unexpected `EmailMessage`
delta, `firewall_breach`-free execution across the observed runs, and
V1/V2 parity confirmed per the evidence capability. No new detection
mechanism is introduced by this requirement.

#### Scenario: Promotable pilot cycle

- GIVEN the pilot ran for the defined minimum volume/duration with the gate
  reporting active throughout
- WHEN the operator evaluates jobs delta, `EmailMessage` delta, and
  `firewall_breach` occurrences across all pilot executions
- THEN a cycle with zero unexpected jobs delta, zero `EmailMessage` delta, and
  zero `firewall_breach` occurrences MUST be marked promotable

#### Scenario: Non-promotable pilot cycle

- GIVEN the pilot ran for the defined minimum volume/duration
- WHEN any execution in the cycle shows a nonzero jobs delta, a nonzero
  `EmailMessage` delta, or a `firewall_breach` classification
- THEN the cycle MUST NOT be marked promotable, regardless of how many other
  executions in the same cycle passed

### Requirement: Abort Criteria And Directed Disposition

Abort MUST be scoped to the actual blast radius of the triggering signal. A
user-specific issue (for example, that single user's `row_limit_exceeded` or
`request_not_allowed` pattern, or a user-specific data problem) MUST result in
removing only that user's id from `BULK_PROCESSING_V2_CANARY_USER_IDS`,
leaving the rest of the cohort active. A `firewall_breach` classification, a
nonzero jobs delta, or a nonzero `EmailMessage` delta MUST result in aborting
the entire pilot: deactivating `BULK_PROCESSING_ENGINE_V2` and
`BULK_PROCESSING_V2_CANARY_ENABLED` and clearing both allowlists, using the
same flags-only rollback pattern already proven for canary rollback. Rows
created by any aborted or completed pilot execution MUST be disposed via the
same directed disposition already proven for canary rows: delete the
associated `BulkSend`/`BulkSendRecipient` rows and the `recipients_file`
media artifact for that execution, scoped and identity-verified against the
specific pilot run being closed out, only after evidence has been captured
per the evidence capability.

#### Scenario: User-scoped abort for a single-user issue

- GIVEN one cohort user repeatedly triggers a user-specific failure with no
  jobs delta, no `EmailMessage` delta, and no `firewall_breach`
- WHEN the operator evaluates the abort criteria
- THEN only that user's id MUST be removed from
  `BULK_PROCESSING_V2_CANARY_USER_IDS`
- AND the remaining cohort members MUST stay active and unaffected

#### Scenario: Full-pilot abort on firewall breach or nonzero delta

- GIVEN any pilot execution reports a `firewall_breach` classification, a
  nonzero jobs delta, or a nonzero `EmailMessage` delta
- WHEN the operator evaluates the abort criteria
- THEN the entire pilot MUST be aborted: `BULK_PROCESSING_ENGINE_V2` and
  `BULK_PROCESSING_V2_CANARY_ENABLED` set to inactive and both allowlists
  cleared, via flags-only change and Django restart
- AND no cohort member remains authorized after the abort

#### Scenario: Directed disposition after abort or close-out

- GIVEN a pilot execution created `BulkSend`/`BulkSendRecipient` rows and a
  `recipients_file` artifact, and evidence for that execution has been
  captured
- WHEN the operator closes out or aborts that execution
- THEN the runbook MUST delete those specific rows and the `recipients_file`
  artifact, scoped and identity-verified to that execution
- AND MUST NOT delete rows or artifacts belonging to a different, still-open
  pilot execution
