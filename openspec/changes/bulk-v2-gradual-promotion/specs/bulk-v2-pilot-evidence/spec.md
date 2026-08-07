# Bulk V2 Pilot Evidence Specification

## Purpose

Defines the per-execution evidence set and V1/V2 parity judgment for Stage 1
of Bulk Processing Engine V2 gradual promotion. Reuses the existing
structured log lines already emitted by `relay/api.py`
(`"bulk_v2_canary decision=..."` and `"bulk_v2_import ... result=..."`) and
the existing baseline-delta pattern (jobs, `EmailMessage`) already proven
across two production canary cycles. This capability defines no new
detection mechanism; it defines what evidence MUST be captured per pilot
execution and how V1/V2 parity is judged from it. Evidence is import-only:
no requirement or scenario in this spec covers or implies a send outcome.

## Requirements

### Requirement: Per-Execution Evidence Set From Existing Structured Logs

Each pilot import execution MUST have an evidence record built from the two
existing structured log lines already emitted by `relay/api.py`: the
`bulk_v2_canary decision=...` line (decision code, request fingerprint, row
count, `external_calls=0`) and the `bulk_v2_import ... result=...` line
(bulk id, engine, import result/status, row count, ledger count, duration,
spool bytes, `background_jobs=0`, `external_calls=0`). No new log line or
logging mechanism is introduced by this requirement.

#### Scenario: Evidence record built from both existing log lines

- GIVEN a pilot import execution completed (successfully or with errors)
- WHEN the operator compiles the evidence record for that execution
- THEN it MUST include the `bulk_v2_canary` decision code and request
  fingerprint from the canary decision log line
- AND it MUST include the `bulk_v2_import` result/status, row count, and
  ledger count from the import log line
- AND it MUST NOT include raw `client_request_id`, raw user id, or recipient
  row content, matching the existing canary evidence convention

#### Scenario: Evidence record incomplete when either log line is missing

- GIVEN a pilot import execution is being closed out
- WHEN either the `bulk_v2_canary decision=...` line or the
  `bulk_v2_import ... result=...` line cannot be located for that execution
- THEN the evidence record MUST be marked incomplete
- AND that execution MUST NOT be counted toward the promotion evaluation
  until the missing log line is located or the execution is excluded

### Requirement: Baseline-Delta Evidence Reused Unchanged

Each pilot execution's evidence record MUST include the jobs delta and
`EmailMessage` delta computed against a pre-execution baseline, using the
same delta pattern already proven by the canary client
(`ops/bulk_v2_canary_client.py::_assert_expected_delta`): any nonzero delta in
either signal is a firewall breach, evaluated independent of whether the
import itself was classified as allowed.

#### Scenario: Zero-delta evidence recorded for a clean execution

- GIVEN a pilot import execution completed
- WHEN the operator computes `(jobs_after - jobs_before, messages_after -
  messages_before)` for that execution
- THEN the evidence record MUST show `(0, 0)`
- AND the execution MUST NOT be classified as a `firewall_breach`

#### Scenario: Nonzero-delta evidence recorded as firewall breach

- GIVEN a pilot import execution completed
- WHEN either the jobs delta or the `EmailMessage` delta for that execution is
  nonzero
- THEN the evidence record MUST classify that execution as a
  `firewall_breach`
- AND that classification MUST feed the abort criteria defined by the
  `bulk-v2-pilot-cohort` capability

### Requirement: V1/V2 Parity Judgment From Existing Evidence

V1/V2 parity for equivalent input MUST be judged as: identical validation
outcome (same accept/reject decision and same error code, where applicable)
and identical operator-visible import status, comparing the V2 pilot
execution's evidence against the equivalent V1 (legacy engine) outcome for
the same input shape. Parity judgment MUST be derived only from evidence
already defined by this capability and the existing V1 import path; no new
comparison data source is introduced.

#### Scenario: Parity confirmed for equivalent input

- GIVEN a V2 pilot execution and an equivalent V1 execution processed the
  same input shape (same row count and validity profile)
- WHEN the operator compares validation outcome and operator-visible import
  status between the two
- THEN parity MUST be confirmed only if both the validation outcome and the
  operator-visible import status match exactly

#### Scenario: Parity gap recorded when outcomes diverge

- GIVEN a V2 pilot execution and an equivalent V1 execution processed the
  same input shape
- WHEN either the validation outcome or the operator-visible import status
  differs between the two
- THEN the operator MUST record a parity gap for that input shape
- AND a recorded parity gap MUST be treated as evidence against promotion for
  the affected input shape, consistent with the `bulk-v2-pilot-cohort`
  promotion criteria

### Requirement: Evidence Retained Only Per Execution Cycle

Pilot evidence records MUST be retained only for the duration of the
execution cycle they document, matching the disposal pattern already used
for the two prior production canary cycles. Evidence is not retained for a
fixed window beyond the cycle it belongs to; retention of the underlying
`BulkSend`/`BulkSendRecipient` rows themselves is governed by the directed
disposition requirement in the `bulk-v2-pilot-cohort` capability, not by this
requirement.

#### Scenario: Evidence compiled before disposition, not after

- GIVEN a pilot execution's rows are about to be disposed per the
  `bulk-v2-pilot-cohort` directed disposition requirement
- WHEN the operator closes out that execution
- THEN the evidence record MUST be compiled and preserved (per the operator's
  chosen out-of-band record, for example the promotion decision log) before
  disposition runs
- AND no evidence-capture step MUST block on retaining the disposed rows
  themselves beyond that cycle
