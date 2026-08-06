# Bulk V2 Canary Execution Client Specification

## Purpose

Defines the committed, auditable, credential-free canary POST client
(`ops/bulk_v2_canary_client.py`) that exercises the production import-only
canary path. It duplicates no server logic; it adds gate-guarded refusal,
client-layer defense-in-depth, and safe logging around the existing V2
request path (`relay/api.py::_bulk_send_create`, unchanged).

## Requirements

### Requirement: Gate-Guarded Execution Refusal

The client MUST evaluate `td02c_settings_gate.evaluate_django_settings` (or
`evaluate_effective_settings`) with `expect_active=True` before constructing
or sending any request, and MUST refuse to run if the result is not allowed.

#### Scenario: Gate passes, client proceeds

- GIVEN `evaluate_django_settings(settings, expect_active=True).allowed == True`
- WHEN the operator invokes the client
- THEN the client MUST proceed to build and send the import-only POST

#### Scenario: Gate fails, client refuses

- GIVEN the effective settings do not satisfy the active gate
- WHEN the operator invokes the client
- THEN the client MUST refuse to send any request and report the gate's failure reasons, without attempting a POST

### Requirement: Credential-Free Execution

The client MUST NOT hardcode credentials, accept them via argv, or write them
to logs. It MUST source auth material from environment variables or a
mode-0600 file outside version control, matching the
`ops/td02c_http_client.py` pattern (secret-bearing curl config file, argv
without secrets).

#### Scenario: Credentials sourced outside argv and logs

- GIVEN credential material is set via environment variable or a mode-0600 local file
- WHEN the client builds its request
- THEN the resulting process argv MUST contain no secret values
- AND no log line MUST contain the credential value

#### Scenario: Missing credential source causes refusal

- GIVEN no credential environment variable or file is present
- WHEN the operator invokes the client
- THEN the client MUST refuse to run and report the missing credential source

### Requirement: Client-Layer Import-Only Enforcement

`import_only=True` is not a request field — `relay/api.py::_bulk_send_create`
hardcodes it server-side as a kwarg to `evaluate_canary`, never reading it
from `request.POST`. As defense-in-depth independent of that server-side
enforcement, the client MUST never send `send_now=True` and MUST never send a
non-empty `scheduled_at`, and MUST expose no configuration option that would
let either be set.

#### Scenario: Client never sends parameters that could trigger a send

- GIVEN the gate passed and the client is about to send a request
- WHEN the request payload is constructed
- THEN it MUST include `send_now=False` and an empty `scheduled_at`
- AND the client MUST expose no option that overrides either value

### Requirement: Fingerprint-Only Logging

The client MUST log a fingerprint of `client_request_id` (matching the
sha256[:12] pattern used in `relay/api.py`), never the raw request id, user
id, or recipient data.

#### Scenario: Log line contains fingerprint, not raw id

- GIVEN the client sends a canary request for a given `client_request_id`
- WHEN the client logs the outcome
- THEN the log line MUST contain the derived fingerprint
- AND MUST NOT contain the raw `client_request_id`, raw user id, or recipient row content

### Requirement: Testable in Isolated Profile

The client's gate-refusal logic and request construction MUST be covered by
unit tests runnable under the `ops/tests/` isolated PostgreSQL test profile,
without network access or production credentials.

#### Scenario: Gate-refusal logic tested without network access

- GIVEN a test settings object representing an inactive gate state
- WHEN the unit test invokes the client's refusal check
- THEN it MUST assert refusal with zero network calls
- AND the test MUST pass under `ops.deployment_test_profile` isolation
