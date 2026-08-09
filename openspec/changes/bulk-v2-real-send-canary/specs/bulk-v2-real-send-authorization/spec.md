# Bulk V2 Real-Send Authorization Specification

## Purpose

Defines the independent authorization gate that must permit — or, by
default, refuse — a V2 real-send attempt. This capability governs six
independent flags, the `evaluate_real_send` gate function, the
operator-only management command's fail-closed preconditions, and the
exact one-email scope of the canary. It shares no parameter, no
return-type semantics, and no fallback with the existing import-only
`evaluate_canary` (`relay/services/bulk_v2_canary.py:42-112`); only the
pure `normalize_allowlist` helper (`relay/services/bulk_v2_canary.py:16-39`)
is reused, because it carries no canary policy of its own.

This capability governs authorization only — decision, not delivery. The
send state machine that governs what happens per recipient once
authorization has cleared is specified separately in
`bulk-v2-send-state-machine`; the structured logging this gate must
produce is specified separately in `bulk-v2-real-send-observability`.

Out of scope for this capability, and therefore not a source of any
requirement below: any change to V1's send pipeline, to `evaluate_canary`,
or to `BULK_PROCESSING_V2_CANARY_MAX_ROWS` (stays 20, import-only, never a
send limit); V2 scheduled send; V2 batch real-send above one recipient;
making V2 the default engine; retiring V1; automatic send triggered by
import; automatic retry of an `ambiguous` outcome; and any real email
send, flag activation, or production mutation as part of this SDD
change's `sdd-apply`. Completing this capability's code, tests, and
command scaffolding during `sdd-apply` is never, by itself, authorization
to activate a flag or execute a send — that separate authorization is
external to this change.

## Requirements

### Requirement: Independent Real-Send Gate With No Shared State Or Fallback

A new `evaluate_real_send(...)` function MUST exist in a module dedicated
to real-send authorization, separate from
`relay/services/bulk_v2_canary.py`. It MUST NOT share a parameter, a
return-type contract, or a fallback path with `evaluate_canary`. It MAY
reuse only `normalize_allowlist`, because that helper contains no
canary-specific policy.

#### Scenario: Gate function is structurally independent

- GIVEN the real-send gate module is inspected
- WHEN it is compared against
  `relay/services/bulk_v2_canary.py::evaluate_canary`
- THEN `evaluate_real_send` MUST be a distinct function with its own
  parameter list and its own return contract
- AND no code path MUST call `evaluate_canary` from `evaluate_real_send`,
  or vice versa, as a fallback

### Requirement: Six Independent Flags And A Single Kill Switch

Real-send authorization MUST be governed by six settings, each
independent of the import canary flags: `BULK_PROCESSING_V2_REAL_SEND_ENABLED`,
`BULK_PROCESSING_V2_REAL_SEND_USER_IDS`,
`BULK_PROCESSING_V2_REAL_SEND_REQUEST_IDS`,
`BULK_PROCESSING_V2_REAL_SEND_TEMPLATE_IDS`,
`BULK_PROCESSING_V2_REAL_SEND_RECIPIENT_DOMAINS`, and
`BULK_PROCESSING_V2_REAL_SEND_MAX_ROWS`.
`BULK_PROCESSING_V2_REAL_SEND_ENABLED` MUST act as the kill switch: when
it is false, no other flag combination MUST authorize a send, and no
in-flight state MUST bypass it.

#### Scenario: All six flags authorize a send

- GIVEN `BULK_PROCESSING_V2_REAL_SEND_ENABLED` is true and the user id,
  request id, template id, and recipient domain each match their
  respective allowlist
- WHEN `evaluate_real_send` evaluates the request
- THEN it MUST return an authorized decision

#### Scenario: Kill switch overrides every other flag and any in-flight state

- GIVEN `BULK_PROCESSING_V2_REAL_SEND_ENABLED` is false, regardless of the
  state of the other five flags or any row already in `sending`
- WHEN `evaluate_real_send` evaluates any request, or the management
  command attempts to start a new attempt
- THEN authorization MUST be refused and no new outbound call MUST be
  initiated

#### Scenario: Any single missing allowlist match refuses authorization

- GIVEN `BULK_PROCESSING_V2_REAL_SEND_ENABLED` is true but the user id,
  the request id, the template id, or the recipient domain fails to
  match its allowlist
- WHEN `evaluate_real_send` evaluates the request
- THEN it MUST return a refused decision with a code identifying which
  check failed

### Requirement: Template Authorization Keyed On template_id Only

The template allowlist MUST be keyed on `template_id`, because that is
the only template-identifying value Doppler receives
(`"templateId": str(template_id)`, `relay/services/doppler_relay.py:614`).
`template_name` MUST NOT be used as an authorization input; it remains
display metadata only (`relay/api.py:400-406`).

#### Scenario: template_id match authorizes, template_name is irrelevant to the decision

- GIVEN a `BulkSend` has `template_id` present in
  `BULK_PROCESSING_V2_REAL_SEND_TEMPLATE_IDS` and any arbitrary
  `template_name`
- WHEN `evaluate_real_send` evaluates the request
- THEN the template check MUST pass based on `template_id` alone

#### Scenario: template_id mismatch refuses authorization regardless of template_name

- GIVEN a `BulkSend` has a `template_id` absent from
  `BULK_PROCESSING_V2_REAL_SEND_TEMPLATE_IDS`
- WHEN `evaluate_real_send` evaluates the request
- THEN authorization MUST be refused even if `template_name` matches an
  authorized-looking value

### Requirement: Recipient Authorization By Domain, Config-Driven, No Hardcoded Address

The recipient allowlist MUST be evaluated by domain against
`BULK_PROCESSING_V2_REAL_SEND_RECIPIENT_DOMAINS`. No individual recipient
email address MUST be hardcoded anywhere in the repository, including
tests, settings defaults, and the management command.

#### Scenario: Recipient domain match authorizes

- GIVEN the sole eligible recipient's normalized domain is present in
  `BULK_PROCESSING_V2_REAL_SEND_RECIPIENT_DOMAINS`
- WHEN `evaluate_real_send` evaluates the request
- THEN the recipient-domain check MUST pass

#### Scenario: Recipient domain mismatch refuses authorization

- GIVEN the sole eligible recipient's normalized domain is absent from
  `BULK_PROCESSING_V2_REAL_SEND_RECIPIENT_DOMAINS`
- WHEN `evaluate_real_send` evaluates the request
- THEN authorization MUST be refused

### Requirement: MAX_ROWS Fixed At 1, Independent From The Import Canary Limit

`BULK_PROCESSING_V2_REAL_SEND_MAX_ROWS` MUST be `1`. It MUST NOT be
satisfied, widened, or bypassed by `BULK_PROCESSING_V2_CANARY_MAX_ROWS`
(which stays 20 and governs import only, never send).

#### Scenario: Exactly one eligible row is authorized

- GIVEN a `BulkSend` has exactly one recipient row eligible for send
- WHEN `evaluate_real_send` evaluates the request
- THEN the row-count check MUST pass against
  `BULK_PROCESSING_V2_REAL_SEND_MAX_ROWS == 1`

#### Scenario: A second eligible row refuses authorization

- GIVEN a `BulkSend` has two or more recipient rows eligible for send
- WHEN `evaluate_real_send` evaluates the request
- THEN authorization MUST be refused, and the 20-row
  `BULK_PROCESSING_V2_CANARY_MAX_ROWS` MUST NOT be read as if it
  authorized the extra rows

### Requirement: Management Command Requires An Explicit bulk_send_id

The operator-only real-send management command MUST require an explicit
`bulk_send_id` argument. It MUST NOT offer an implicit "latest" or "all"
mode.

#### Scenario: Command invoked without bulk_send_id fails before touching any row

- GIVEN the operator invokes the management command without a
  `bulk_send_id` argument
- WHEN the command parses its arguments
- THEN it MUST fail before evaluating the gate or reading any
  `BulkSendRecipient` row

### Requirement: Management Command Fails Closed On Engine, Gate, And Allowlist Mismatch

Before processing any row, the management command MUST fail closed if any
of the following is true: the targeted `BulkSend.engine_version !=
"v2"`; `evaluate_real_send` does not authorize the request; or the
targeted `BulkSend`'s user, `client_request_id`, `template_id`, or
recipient domain does not exactly match the allowlists.

#### Scenario: Legacy-engine BulkSend is refused

- GIVEN the operator targets a `BulkSend` with
  `engine_version == "legacy"`
- WHEN the command runs its preconditions
- THEN it MUST fail closed before any gate evaluation or Doppler call

#### Scenario: Gate refusal stops the command

- GIVEN `evaluate_real_send` returns a refused decision for the targeted
  `BulkSend`
- WHEN the command runs its preconditions
- THEN it MUST stop with no row processed and no Doppler call made

#### Scenario: Allowlist mismatch on any single dimension stops the command

- GIVEN the targeted `BulkSend`'s user, `client_request_id`,
  `template_id`, or recipient domain does not exactly match its
  respective allowlist
- WHEN the command runs its preconditions
- THEN it MUST stop with no row processed and no Doppler call made,
  regardless of which single dimension mismatched

### Requirement: Management Command Forbids Retry, Force, And Wildcard Overrides, And Never Reads The CSV

The management command MUST NOT offer a `--retry-ambiguous` flag, MUST
NOT offer a `--force` flag, MUST NOT accept a recipient wildcard of any
kind, and MUST NOT parse, open, or read the CSV in any way. It MUST
select recipients exclusively from persisted `BulkSendRecipient` rows.

#### Scenario: No override flags exist in the command's interface

- GIVEN the management command's argument parser is inspected
- WHEN its accepted arguments are enumerated
- THEN no `--retry-ambiguous`, `--force`, or wildcard-recipient argument
  MUST be present

#### Scenario: Command never touches the CSV

- GIVEN a `BulkSend` whose original `recipients_file` is present,
  missing, or corrupted
- WHEN the management command processes the targeted `BulkSend`
- THEN the command's behavior MUST be identical in all three cases,
  because it MUST NOT open, parse, or depend on `recipients_file` at
  send time

### Requirement: Canary Scope Is Exactly One Email, Zero Scheduling, Zero Concurrency, Zero Automatic Retry

The real-send canary MUST be scoped to exactly one authorized user, one
`client_request_id`, one `template_id`, one recipient domain, and
`BULK_PROCESSING_V2_REAL_SEND_MAX_ROWS == 1`. It MUST NOT support
scheduled send, MUST NOT be designed to run more than one worker against
the canary concurrently, and MUST NOT automatically retry any outcome.

#### Scenario: Scope is provably minimal end to end

- GIVEN the canary is authorized and the management command runs
- WHEN the full authorization-to-terminal-outcome path is exercised
- THEN at most one Doppler call MUST occur across the entire execution,
  with no scheduling parameter honored and no automatic retry triggered
  by any outcome

### Requirement: Completing This Capability Is Never Authorization To Execute A Real Send

Delivering the code, tests, settings scaffolding, and management command
in this change (build) MUST NOT itself activate any flag, execute any
send, or touch production. Execution (activating flags, choosing the
concrete authorized values, running the command against a real
recipient) requires separate, explicit, later authorization outside this
SDD change.

#### Scenario: sdd-apply completion leaves the system unable to send

- GIVEN `sdd-apply` for this change has completed, including PR2's
  send-capable code path
- WHEN the deployed configuration is inspected immediately after apply
- THEN `BULK_PROCESSING_V2_REAL_SEND_ENABLED` MUST still be false (or
  absent), no allowlist MUST contain a production value, and zero real
  Doppler calls MUST have occurred as a result of this change's delivery
