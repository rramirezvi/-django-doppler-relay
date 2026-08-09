# Bulk V2 Real-Send Observability Specification

## Purpose

Defines the minimum logging fix and the three structured events required
to make a real-send attempt observable in production. Today
`config/settings.py` has no `LOGGING` dict, so Django's default leaves the
root logger at `WARNING` and every `logger.info()` call in `relay/` is
discarded inside the process before it ever reaches stderr, gunicorn, or
journald — even though the systemd/journald wiring documented in
`DEPLOY.md` is otherwise correct and would carry the record if it were
emitted at a level the logger accepts. This capability is PR1 scope: it
carries no send capability by itself and MUST be deliverable and testable
before any send-capable code exists.

Out of scope for this capability: any change to what V1 logs today; any
new logging destination beyond the existing gunicorn stderr -> systemd
journal -> `journalctl -u django.service` pipeline; and any log content
that would defeat the redaction requirements below.

## Requirements

### Requirement: INFO-Level relay Logs Must Reach journald Via A LOGGING Configuration

`config/settings.py` MUST define a `LOGGING` dict that configures the
`relay` logger at `INFO` with a handler that reaches process stderr, so
the existing gunicorn -> systemd -> journald pipeline carries it. This
requirement MUST be satisfied independently of, and before, any real-send
code path exists (PR1 scope) — it is a prerequisite for observing PR2's
events, not bundled with them.

#### Scenario: An INFO log from relay is observable via journalctl

- GIVEN the `LOGGING` dict is applied and `django.service` is running
- WHEN any code under `relay/` calls `logger.info(...)`
- THEN the resulting record MUST be retrievable via
  `journalctl -u django.service`, where today it would be silently
  discarded

#### Scenario: The logging fix requires no send capability to verify

- GIVEN only PR1 has been applied (no send-capable code path exists yet)
- WHEN the `LOGGING` configuration is tested
- THEN the test MUST be able to prove `relay` INFO logs reach the
  configured handler using only pre-existing, non-send log statements —
  no real-send code is required to validate this requirement

### Requirement: Decision Event Recorded For Every Gate Evaluation

Every evaluation of `evaluate_real_send` MUST emit a structured
`bulk_v2_real_send_decision` event at `INFO`, carrying at minimum: a
request fingerprint, the `BulkSend` id, the decision outcome and its code,
and the relevant timestamps. This applies whether the decision authorizes
or refuses the request.

#### Scenario: An authorized decision is logged

- GIVEN `evaluate_real_send` authorizes a request
- WHEN the decision is made
- THEN a `bulk_v2_real_send_decision` event MUST be emitted with the
  `BulkSend` id and an authorized outcome code

#### Scenario: A refused decision is logged with its refusal code

- GIVEN `evaluate_real_send` refuses a request for any reason (kill
  switch, allowlist mismatch, row-count mismatch)
- WHEN the decision is made
- THEN a `bulk_v2_real_send_decision` event MUST be emitted carrying the
  specific refusal code, so an operator can distinguish why authorization
  failed without reading source code

### Requirement: Attempt Event Recorded Before Every Outbound Call

Before the worker makes the single outbound Doppler call for a claimed
row, it MUST emit a structured `bulk_v2_real_send_attempt` event at
`INFO`, carrying at minimum: the `BulkSend` id, the recipient's
idempotency key, the `BackgroundJob` id, and the attempt number.

#### Scenario: Attempt event precedes the outbound call

- GIVEN a worker has claimed a row and committed its `sending` transition
- WHEN it is about to make the Doppler call
- THEN a `bulk_v2_real_send_attempt` event MUST have already been emitted
  for that attempt, correlated to the same `BulkSend` id, recipient
  idempotency key, `BackgroundJob` id, and attempt number that were
  persisted in the `sending` transition

### Requirement: Result Event Recorded For Every Terminal Or Ambiguous Outcome

Every terminal transition (`sent`, `send_failed`) and every `ambiguous`
outcome MUST emit a structured `bulk_v2_real_send_result` event at `INFO`,
carrying at minimum: the same correlation fields as the attempt event,
plus the result, the error code when applicable, the Doppler message
identifier when present, and the relevant timestamps.

#### Scenario: A sent result is logged with the Doppler identifier

- GIVEN a row reaches `send_status == "sent"`
- WHEN the result is persisted
- THEN a `bulk_v2_real_send_result` event MUST be emitted carrying the
  Doppler-returned identifier and the same correlation fields as the
  attempt event for that recipient

#### Scenario: An ambiguous result is logged distinctly from a definitive failure

- GIVEN a row reaches `send_status == "ambiguous"` rather than
  `send_status == "send_failed"`
- WHEN the result is persisted
- THEN the `bulk_v2_real_send_result` event MUST clearly mark the outcome
  as `ambiguous`, distinguishable from a definitive `send_failed` result,
  so an operator scanning logs does not conflate the two

### Requirement: Forbidden Content Is Never Logged By Any Of The Three Events

None of `bulk_v2_real_send_decision`, `bulk_v2_real_send_attempt`, or
`bulk_v2_real_send_result` MUST ever include: the Doppler API key or any
raw token, the full recipient email address where an identifier or
domain-only representation would suffice, the message body, any CSV
content, or sensitive template variable values.

#### Scenario: A logged event contains no secret or raw token

- GIVEN any of the three events is emitted during a real-send attempt
- WHEN its payload is inspected
- THEN it MUST NOT contain `settings.DOPPLER_RELAY["API_KEY"]`,
  `DOPPLER_RELAY_API_KEY`, or any other raw credential or token value

#### Scenario: A logged event avoids the full recipient address where avoidable

- GIVEN any of the three events references the recipient
- WHEN its payload is inspected
- THEN it MUST identify the recipient via the recipient's idempotency key
  or an equivalent non-reversible-enough reference rather than the full
  email address, wherever a full address is avoidable

#### Scenario: A logged event contains no message body, CSV content, or template variables

- GIVEN any of the three events is emitted
- WHEN its payload is inspected
- THEN it MUST NOT contain the rendered message body, any raw CSV row or
  file content, or the values of any template variable
