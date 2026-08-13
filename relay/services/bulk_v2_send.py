"""bulk-v2-real-send-canary V2 send engine (design.md §10, §14).

First Doppler-capable module in this change. `process_bulk_id_v2` is the
function `relay/services/jobs.py::dispatch_background_job` routes the new
`BackgroundJob.TYPE_BULK_SEND_V2_REAL` job type to.

Hard structural constraints, each independently verifiable and each
enforced/tested by PR2b-T35's suite-wide no-real-HTTP guard and the grep
checks referenced inline below:
  - Recipients are read EXCLUSIVELY from `BulkSendRecipient` via
    `bulk_v2_send_state.claim_next_recipient` (PR2a). Zero import of `csv`,
    zero reference to `bulk.recipients_file` or `BulkImportService`
    anywhere in this module (design §6 "Zero CSV dependency is
    structural").
  - `relay/services/bulk_processing.py` is not imported and is not
    modified — this module is intentionally separate (design §14's
    refinement of the proposal), so the legacy guard at
    `bulk_processing.py:39-42` is untouched by inspection.
  - Transport invariant (fix-bulk-v2-template-variable-validation
    formalizes what "exactly one Doppler call" meant pre-gate — that
    phrase alone is no longer precise, since a successful run now makes
    TWO distinct HTTP calls, of two different kinds, at two different
    scopes):
      1. discovery succeeds + payload valid -> exactly 1 read-only GET
         (`get_required_template_variables`, once per `BulkSend`, never
         per recipient) + exactly 1 send POST per claimed recipient.
      2. variables missing -> exactly 1 GET, 0 POST.
      3. discovery failed (content indeterminate) -> 1 GET attempted
         (its failure is what's being reported), 0 POST.
      4. no eligible recipient / no-op re-run -> 0 GET, 0 POST (the GET
         is skipped entirely — see `eligible_pks` check below).
      5. under no circumstance can there be more than 1 send POST per
         recipient per invocation: the single-attempt client
         (`build_single_attempt_client`) still prevents transport-layer
         retry on the SEND call specifically (doppler_relay.py's
         `_request` loop runs once for `max_attempts=1`), and this
         module still adds no retry loop of its own on top of that —
         the single-attempt guarantee is per-recipient-send and is
         unweakened by the gate.
      6. the discovery GET never touches `BulkSendRecipient` at all: it
         cannot increment `send_attempt_number`, change `send_status`,
         or cause a `sending` transition — it runs before
         `claim_next_recipient` is ever called (see below).
      7. any discovery failure happens strictly before any row is
         claimed — `eligible_pks` and the gate are resolved first, so a
         rejected `BulkSend` never has a row sitting in `sending`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests
from django.conf import settings
from django.utils import timezone

from relay.models import BulkSend, BulkSendRecipient
from relay.services.bulk_v2_send_state import (
    SendStateError,
    claim_next_recipient,
    describe_send_ledger,
    ensure_autocommit_context,
    mark_ambiguous,
    mark_send_failed,
    mark_sent,
)
from relay.services.doppler_relay import DopplerRelayClient, DopplerRelayError

logger = logging.getLogger(__name__)

_MUSTACHE_VAR_RE = re.compile(r"\{\{([^}]+)\}\}")
_VALID_VAR_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*[a-zA-Z0-9_]$")


class TemplateVariableDiscoveryError(RuntimeError):
    """The real Mustache variable set for a template could not be obtained
    with confidence (e.g. `get_template_html` returned empty, or the
    request itself failed). Never treated as "zero variables required" —
    that conflation is exactly the pre-existing V1 bug this module avoids
    repeating (see `RealSendTemplateDiscoveryFailed`'s docstring)."""


class RealSendTemplateVariablesMissing(RuntimeError):
    """Raised by `process_bulk_id_v2` before any recipient is claimed: the
    template's real content was read successfully and its required
    variables were determined, but at least one eligible recipient's
    persisted `payload` does not cover all of them. The template-discovery
    GET already happened (that's how the mismatch was found); zero send
    POST is made and zero recipient row is touched — `BulkSendRecipient`
    stays `not_started`.
    Caught by `run_claimed_job` (jobs.py) exactly like any other dispatch
    exception, which sets the `BackgroundJob` to `STATE_ERROR`."""


class RealSendTemplateDiscoveryFailed(RuntimeError):
    """Raised by `process_bulk_id_v2` before any recipient is claimed: the
    template's real required variables could not be determined at all
    (fail-closed — see `TemplateVariableDiscoveryError`). Distinct from
    `RealSendTemplateVariablesMissing` because no variable list exists to
    report here. Same zero-touch/BackgroundJob-error handling."""


def get_required_template_variables(
    client: DopplerRelayClient, account_id: int, template_id: str
) -> frozenset[str]:
    """V2-isolated Mustache variable discovery. Reuses the already-working
    `DopplerRelayClient.get_template_html` (which correctly follows the
    `_links[get-template-body]` relation `get_template_fields` does not —
    see `V1_TEMPLATE_VARIABLE_VALIDATION_BROKEN`, registered separately and
    left unfixed on purpose: this function duplicates only the small,
    already-validated Mustache-extraction regex instead of touching
    `doppler_relay.py`, so V1's `get_template_fields`/`process_bulk_id`
    remain byte-identical.

    `get_template_html` fails "open" to `""` on any error (its own
    `except Exception: return ""`), which does not distinguish "genuinely
    no content" from "could not determine content" — this function refuses
    to inherit that ambiguity and raises `TemplateVariableDiscoveryError`
    on empty content instead of returning an empty variable set."""
    html = client.get_template_html(account_id, template_id)
    if not html:
        raise TemplateVariableDiscoveryError(
            f"No se pudo obtener contenido real de la plantilla {template_id}."
        )
    variables: set[str] = set()
    for match in _MUSTACHE_VAR_RE.finditer(html):
        name = match.group(1).strip()
        if _VALID_VAR_NAME_RE.match(name):
            variables.add(name)
    return frozenset(variables)


def build_single_attempt_client() -> DopplerRelayClient:
    """design.md §10 (D9) — V2 real-send always uses a single-attempt
    client. `_request`'s transport-layer retry loop never fires for a
    real-send attempt; this module additionally never wraps the call in a
    retry loop of its own (see module docstring)."""
    return DopplerRelayClient(max_attempts=1)


def _domain_of(normalized_recipient: str) -> str:
    text = normalized_recipient or ""
    if "@" not in text:
        return ""
    return text.rsplit("@", 1)[-1]


def _build_recipients_model(bulk: BulkSend, recipient_email: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Builds the minimal `recipients_model` shape `send_template_message`
    expects, reading only already-persisted `BulkSend`/`BulkSendRecipient`
    fields plus the existing `DEFAULT_FROM_EMAIL`/`DEFAULT_FROM_NAME`
    fallback (the same fallback pattern already used at
    relay/views.py:423-424). No duplicated payload builder, no CSV access,
    no new business logic beyond what is already persisted.

    NOTE (discovered gap, flagged explicitly — design.md does not specify
    this mapping): `BulkSend` has no stored "owner"/from-address field of
    its own; V1's process_bulk_template_send derives from_email/from_name
    from the acting user's UserEmailConfig or explicit request input,
    neither of which exists in a bare `manage.py` invocation. This
    function therefore falls back to the account-level
    DOPPLER_RELAY DEFAULT_FROM_EMAIL/DEFAULT_FROM_NAME settings, which is
    the same fallback V1 itself already uses when no per-user config is
    available. If those settings are empty, `send_template_message` raises
    ValueError("from_email es requerido") before any socket write, which
    the classifier below correctly maps to send_failed/payload_invalid —
    a fail-closed outcome, not a silent one.
    """
    cfg = settings.DOPPLER_RELAY
    return {
        "from_email": cfg.get("DEFAULT_FROM_EMAIL", ""),
        "from_name": cfg.get("DEFAULT_FROM_NAME", ""),
        "subject": bulk.subject or "",
        "recipients": [
            {"email": recipient_email, "name": "", "variables": payload or {}},
        ],
    }


def _classify_outcome(
    exc: Exception | None, result: dict[str, Any] | None
) -> tuple[str, str, str, str]:
    """design.md §10.1 — the exact mapping from what happened at the call
    site to (send_status_bucket, error_code, message_id, location).
    `send_status_bucket` is one of "sent" / "send_failed" / "ambiguous".

    Ordering is load-bearing: `requests.exceptions.JSONDecodeError` (what
    `response.json()` actually raises in this repo's installed `requests`
    version) is ALSO a subclass of `requests.exceptions.RequestException`
    and of `ValueError`, so the JSON-decode check must be evaluated before
    the RequestException/plain-ValueError checks below it, or a genuinely
    unparseable response would be misclassified as a definitive
    request-level failure instead of `ambiguous`/`response_unparseable`.
    """
    if exc is None:
        resultados = (result or {}).get("resultados") or []
        message_id = ""
        if resultados and isinstance(resultados[0], dict):
            message_id = resultados[0].get("message_id") or ""
        # design.md §11.1: send_template_message's TRANSFORMED return
        # value ({"ok":..., "resultados":..., "total":...}) does not
        # expose the raw Location response header — only a per-recipient
        # message_id survives the transformation (doppler_relay.py's
        # local `result["_location"]` variable is never returned). This
        # is a discovered gap between design.md §11.1's code excerpt
        # (which reads response.json() directly) and the actual,
        # unmodified send_template_message contract this module is bound
        # to call without duplicating a payload builder (design §10's
        # explicit rejection of a parallel extraction). `send_location`
        # is therefore always persisted as "" under the current, unmodified
        # send_template_message signature. Flagged explicitly, not fixed
        # here, per the two-line-change boundary (design §10, §14).
        return "sent", "", message_id, ""

    if isinstance(exc, DopplerRelayError):
        status = exc.status
        response_data = exc.payload.get("response") if isinstance(exc.payload, dict) else None
        if status == 402 and isinstance(response_data, dict) and response_data.get("errorCode") == 1:
            return "send_failed", "doppler_quota_exceeded", "", ""
        if status is None:
            return "ambiguous", "doppler_error_no_status", "", ""
        if status in (408, 429) or status >= 500:
            return "ambiguous", f"doppler_http_{status}", "", ""
        if 400 <= status < 500:
            return "send_failed", f"doppler_http_{status}", "", ""
        return "ambiguous", f"doppler_http_{status}", "", ""

    if isinstance(exc, json.JSONDecodeError):
        return "ambiguous", "response_unparseable", "", ""

    if isinstance(exc, requests.Timeout):
        return "ambiguous", "timeout", "", ""
    if isinstance(exc, requests.ConnectionError):
        return "ambiguous", "connection_error", "", ""
    if isinstance(exc, requests.RequestException):
        return "ambiguous", "request_exception", "", ""

    # Payload validation inside send_template_message itself, raised
    # before any socket write (e.g. "from_email es requerido"). Uses an
    # exact-type check (not isinstance) because json.JSONDecodeError is
    # ALSO a ValueError subclass and is already handled above.
    if type(exc) is ValueError:
        return "send_failed", "payload_invalid", "", ""

    # Any other exception, explicitly including AttributeError — the
    # pre-existing V1 `_request` terminal-wrapper defect (see
    # doppler_relay.py's comment near its terminal DopplerRelayError raise)
    # surfaces exactly this way on a network error. This broad catch-all
    # is deliberate: it is what guarantees a row is NEVER left silently
    # stuck in `sending` because of an exception type this classifier
    # did not anticipate.
    return "ambiguous", "dispatch_exception", "", ""


def process_bulk_id_v2(bulk_send_id: int, *, job_id: int) -> str:
    """V2 real-send engine (design.md §10, §14).

    Reads recipients EXCLUSIVELY from `BulkSendRecipient` via
    `claim_next_recipient` — never opens, parses, or references the CSV /
    `bulk.recipients_file` / `BulkImportService` in any way. Only rows
    with `status=pending` AND `send_status=not_started` are eligible; that
    filtering already happens inside `claim_next_recipient` (PR2a) and is
    not duplicated or second-guessed here.

    Loops claiming and processing rows until `claim_next_recipient`
    returns None (design.md §10.1's PR2b-T1 pseudocode: "Loop: ...; if
    None, stop"), so the safety properties hold generally and are not
    conditioned on `BULK_PROCESSING_V2_REAL_SEND_MAX_ROWS == 1` (per the
    state-machine spec's explicit requirement). For THIS canary, the
    `MAX_ROWS == 1` ceiling is enforced upstream, before this function is
    ever invoked, by `evaluate_real_send` + the management command's
    ledger checks — so in practice the loop below claims at most one row.

    At most ONE send POST is made per claimed row (the single-attempt
    client already prevents transport-layer retry, and this function adds
    no retry loop of its own on top) — see the module docstring's
    "Transport invariant" for the full picture, which also now includes
    at most one read-only, per-`BulkSend` (not per-row) template-discovery
    GET before the first row is ever claimed.
    """
    ensure_autocommit_context()

    bulk = BulkSend.objects.only("template_id", "subject").get(pk=bulk_send_id)
    account_id = settings.DOPPLER_RELAY.get("ACCOUNT_ID", 0)
    client = build_single_attempt_client()

    # Template-variable gate (fix-bulk-v2-template-variable-validation):
    # runs once per BulkSend, before the first `claim_next_recipient` call,
    # so a mismatch never claims/touches any row. Placed inside this
    # function (not only in the management command) because the dispatcher
    # bypass path (jobs.py's `TYPE_BULK_SEND_V2_REAL` branch, exercised by
    # `run_background_job(job_id)`) reaches `process_bulk_id_v2` directly —
    # the same defense-in-depth reasoning as the per-recipient CAS.
    #
    # `eligible_pks` is resolved FIRST, before any Doppler traffic: a run
    # with nothing left to send (e.g. a repeated/no-op invocation after a
    # prior success) must make zero Doppler calls of any kind, exactly as
    # before this gate existed — reusing `describe_send_ledger` here (the
    # same read-only classifier the management command's own checks use)
    # means the template-discovery GET is skipped entirely rather than
    # firing needlessly on every re-invocation.
    eligible_pks = [row.pk for row in describe_send_ledger(bulk_send_id).eligible]

    if eligible_pks:
        try:
            required_vars = get_required_template_variables(
                client, account_id, bulk.template_id
            )
        except TemplateVariableDiscoveryError as exc:
            logger.info(
                "bulk_v2_real_send_template_check bulk_send_id=%s job_id=%s "
                "code=real_send_template_discovery_failed template_id=%s",
                bulk_send_id, job_id, bulk.template_id,
            )
            raise RealSendTemplateDiscoveryFailed(
                "No se pudieron determinar las variables requeridas de la "
                f"plantilla {bulk.template_id}."
            ) from exc

        # `email` is deliberately excluded from the coverage check:
        # `_build_recipients_model` always supplies it as the recipient's
        # own top-level `email` field (never inside `variables`/`payload`
        # — see that function), so a template's `{{email}}` is already
        # structurally satisfied by every real send regardless of what the
        # imported CSV columns were. Treating it as "missing" here would
        # false-positive-block the common case of a template greeting the
        # recipient by their own address.
        checkable_required_vars = required_vars - {"email"}
        missing_vars: set[str] = set()
        payloads = (
            BulkSendRecipient.objects
            .filter(pk__in=eligible_pks)
            .values_list("payload", flat=True)
        )
        for payload in payloads:
            missing_vars |= checkable_required_vars - set((payload or {}).keys())

        if missing_vars:
            logger.info(
                "bulk_v2_real_send_template_check bulk_send_id=%s job_id=%s "
                "code=real_send_template_variables_missing missing_variables=%s",
                bulk_send_id, job_id, ",".join(sorted(missing_vars)),
            )
            raise RealSendTemplateVariablesMissing(
                "Variables requeridas ausentes en el payload: "
                f"{', '.join(sorted(missing_vars))}."
            )

    processed = 0
    while True:
        row_pk = claim_next_recipient(bulk_send_id, job_id=job_id)
        if row_pk is None:
            break

        row = (
            BulkSendRecipient.objects
            .values(
                "idempotency_key", "normalized_recipient", "payload",
                "send_attempt_number", "send_started_at",
            )
            .get(pk=row_pk)
        )
        recipient_key = str(row["idempotency_key"])
        recipient_domain = _domain_of(row["normalized_recipient"])
        attempt_number = row["send_attempt_number"]
        send_started_at = row["send_started_at"]

        # design.md §12.2: emitted immediately after the `sending`
        # transition is committed (claim_next_recipient already committed
        # it above), before the outbound call.
        logger.info(
            "bulk_v2_real_send_attempt bulk_send_id=%s recipient_key=%s "
            "recipient_domain=%s job_id=%s attempt_number=%s send_started_at=%s",
            bulk_send_id,
            recipient_key,
            recipient_domain,
            job_id,
            attempt_number,
            send_started_at.isoformat() if send_started_at else "",
        )

        if getattr(client, "max_attempts", None) != 1:
            raise SendStateError(
                "real send requires a single-attempt Doppler client"
            )

        recipients_model = _build_recipients_model(
            bulk, row["normalized_recipient"] or "", row["payload"] or {}
        )

        exc: Exception | None = None
        result: dict[str, Any] | None = None
        try:
            result = client.send_template_message(
                account_id, bulk.template_id, recipients_model
            )
        except Exception as e:  # noqa: BLE001 - deliberately broad, see _classify_outcome
            exc = e
        finished = timezone.now()

        bucket, error_code, message_id, location = _classify_outcome(exc, result)
        error_message = str(exc)[:255] if exc is not None else ""

        # Terminal transition, its own transaction, never inside any
        # transaction that was open during the call (design §10.1).
        if bucket == "sent":
            mark_sent(row_pk, message_id=message_id, location=location, now=finished)
        elif bucket == "send_failed":
            mark_send_failed(
                row_pk, error_code=error_code, error_message=error_message, now=finished
            )
        else:
            mark_ambiguous(
                row_pk, error_code=error_code, error_message=error_message, now=finished
            )

        duration_ms = int(
            (finished - send_started_at).total_seconds() * 1000
        ) if send_started_at else 0

        # design.md §12.2: emitted after the terminal/ambiguous transition
        # commits.
        logger.info(
            "bulk_v2_real_send_result bulk_send_id=%s recipient_key=%s "
            "recipient_domain=%s job_id=%s attempt_number=%s result=%s "
            "error_code=%s message_id=%s location=%s send_started_at=%s "
            "finished_at=%s duration_ms=%s",
            bulk_send_id,
            recipient_key,
            recipient_domain,
            job_id,
            attempt_number,
            bucket,
            error_code,
            message_id,
            location,
            send_started_at.isoformat() if send_started_at else "",
            finished.isoformat(),
            duration_ms,
        )

        processed += 1

    return f"Real send V2 procesado: {processed} fila(s)"
