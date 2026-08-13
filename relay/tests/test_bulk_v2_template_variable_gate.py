"""fix-bulk-v2-template-variable-validation: tests for the V2-only,
pre-claim template-variable gate added to `process_bulk_id_v2`.

Covers, independently: exact Mustache extraction (casing/dedup), the two
distinct fail-closed outcomes (`RealSendTemplateVariablesMissing` vs
`RealSendTemplateDiscoveryFailed`), the exact first-canary payload as a
named regression, the exact compatible payload as the positive path, zero
Doppler-send/zero-transport-beyond-discovery proof, `BackgroundJob`
terminal-error handling via both the command and the `run_background_job`
bypass path, and that a rejection never blocks a later retry once the
payload is fixed (idempotency/CAS regression).

Every test that can reach `process_bulk_id_v2` inherits
`NoRealDopplerCallTestCase` (PR2b-T35) and configures its own transport
mocks explicitly — `mock_transport(...)` for the SEND path,
`mock_template_transport(...)` for the template-discovery GET path.
"""

from __future__ import annotations

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import override_settings

from relay.models import BackgroundJob, BulkSend, BulkSendRecipient
from relay.services.bulk_import import BulkImportService
from relay.services.bulk_v2_send import (
    RealSendTemplateDiscoveryFailed,
    RealSendTemplateVariablesMissing,
    TemplateVariableDiscoveryError,
    get_required_template_variables,
    process_bulk_id_v2,
)
from relay.services.doppler_relay import DopplerRelayClient
from relay.services.jobs import run_background_job
from relay.tests._bulk_v2_real_send_support import (
    FakeDopplerResponse,
    NoRealDopplerCallTestCase,
    RealSendFixtureMixin,
)

# The exact content of the real "PRUEBA" template, verified live and
# read-only against production Doppler during the first canary's
# post-mortem — 6 Mustache variables, Spanish, specific casing.
PRUEBA_TEMPLATE_HTML = (
    "{{email}}\r\n{{nombre}}\r\n{{cedula}}\r\n{{codigo}}\r\n{{Valor}}\r\n{{Plazo}}"
)

# The exact payload persisted for the first real canary (BulkSend pk=1160
# in production) — the CSV columns that caused the empty-body incident.
FIRST_CANARY_PAYLOAD = {
    "code": "REALCANARY01",
    "name": "Real Send Canary",
    "note": "primer envio real v2",
    "amount": "10.00",
}

# The exact payload that WOULD have satisfied "PRUEBA" (email is excluded
# from the check by design — see bulk_v2_send.py — so it need not appear
# here for the gate to pass, though a real CSV would still include it as
# the recipient's address).
COMPATIBLE_PAYLOAD = {
    "nombre": "Canary V2",
    "cedula": "0999999999",
    "codigo": "REALCANARY02",
    "Valor": "10.00",
    "Plazo": "1",
}


def _template_response(html: str):
    return FakeDopplerResponse(
        status_code=200,
        json_data={
            "id": "tpl-real", "name": "PRUEBA", "subject": "PUEBA",
            "bodyType": "rawHtml", "htmlContent": html,
        },
    )


class GetRequiredTemplateVariablesTests(NoRealDopplerCallTestCase):
    """Direct unit tests of the pure extraction function — items 1-3."""

    def test_extracts_exact_variables_with_casing_preserved_and_deduped(self):
        self.mock_template_transport(return_value=_template_response(
            "{{email}}\r\n{{nombre}}\r\n{{cedula}}\r\n{{codigo}}\r\n"
            "{{Valor}}\r\n{{Plazo}}\r\n{{Valor}}"  # Valor repeated on purpose
        ))
        client = DopplerRelayClient()
        result = get_required_template_variables(client, 1, "tpl-real")

        self.assertEqual(
            result, frozenset({"email", "nombre", "cedula", "codigo", "Valor", "Plazo"})
        )
        # casing preserved: exact-case membership, not a case-insensitive one
        self.assertIn("Valor", result)
        self.assertNotIn("valor", result)
        self.assertIn("Plazo", result)
        self.assertNotIn("plazo", result)

    def test_discovery_failed_when_content_cannot_be_determined(self):
        # No htmlContent/textContent/html/body/content anywhere, no _links
        # with a get-template-body relation — the exact shape that caused
        # the original get_template_fields() bug (see doppler_relay.py).
        self.mock_template_transport(return_value=FakeDopplerResponse(
            status_code=200,
            json_data={"id": "tpl-real", "name": "PRUEBA", "subject": "PUEBA", "bodyType": "rawHtml"},
        ))
        client = DopplerRelayClient()
        with self.assertRaises(TemplateVariableDiscoveryError):
            get_required_template_variables(client, 1, "tpl-real")


class EmailVariableExclusionTests(RealSendFixtureMixin, NoRealDopplerCallTestCase):
    """Formalizes, in code/tests rather than only in a comment, the one
    deliberate exception to "every required variable must be in payload":
    `{{email}}` is satisfied by the recipient's own top-level `email`
    field (`_build_recipients_model` always supplies it outside
    `variables`/`payload`), so it is excluded from the coverage check —
    and ONLY that exact name, nothing else."""

    def test_email_only_template_passes_without_payload_email_key(self):
        """A template needing ONLY {{email}} must pass the gate even
        though `payload` never contains an "email" key at all (by design
        — see RealSendFixtureMixin.make_occurrence's default payload,
        which has no "email" key, and _build_recipients_model, which
        never merges recipient_email into `variables`)."""
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk, payload={"unrelated": "value"})
        self.assertNotIn("email", row.payload)
        self.mock_template_transport(return_value=_template_response("{{email}}"))
        self.mock_transport(return_value=FakeDopplerResponse())

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            result = process_bulk_id_v2(bulk.pk, job_id=1)

        self.assertIn("1 fila", result)
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENT)

    def test_non_email_variables_are_still_required_from_payload(self):
        """The exclusion is exclusively for the literal name "email" —
        every other variable (including one that merely looks related)
        must still be covered by `payload`, or the gate rejects."""
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk, payload={"unrelated": "value"})
        self.mock_template_transport(
            return_value=_template_response("{{email}}\n{{nombre}}")
        )

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            with self.assertRaises(RealSendTemplateVariablesMissing) as cm:
                process_bulk_id_v2(bulk.pk, job_id=1)

        self.assertIn("nombre", str(cm.exception))
        self.assertNotIn("email", str(cm.exception))  # never reported missing
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_NOT_STARTED)

    def test_exclusion_does_not_extend_to_differently_cased_email_variants(self):
        """The exclusion is an exact-string match on "email" — a template
        variable spelled with different casing (e.g. {{Email}}) is a
        DIFFERENT Mustache identifier and is NOT exempted; it must be
        covered by payload like any other variable."""
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk, payload={"unrelated": "value"})
        self.mock_template_transport(return_value=_template_response("{{Email}}"))

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            with self.assertRaises(RealSendTemplateVariablesMissing) as cm:
                process_bulk_id_v2(bulk.pk, job_id=1)

        self.assertIn("Email", str(cm.exception))
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_NOT_STARTED)

    def test_payload_variable_casing_remains_strict_alongside_email_exclusion(self):
        """Sanity check that the email exclusion doesn't loosen casing
        strictness for everything else: payload has lowercase "valor",
        template requires "Valor" — still rejected."""
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk, payload={"valor": "10.00"})
        self.mock_template_transport(return_value=_template_response("{{email}}\n{{Valor}}"))

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            with self.assertRaises(RealSendTemplateVariablesMissing) as cm:
                process_bulk_id_v2(bulk.pk, job_id=1)

        self.assertIn("Valor", str(cm.exception))
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_NOT_STARTED)


class TemplateVariableGateProcessTests(RealSendFixtureMixin, NoRealDopplerCallTestCase):
    """`process_bulk_id_v2`-level gate behavior — items 4-9."""

    # --- item 7: exact first-canary payload as a named regression ----------

    def test_first_canary_payload_is_rejected_as_variables_missing(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk, payload=dict(FIRST_CANARY_PAYLOAD))
        self.mock_template_transport(return_value=_template_response(PRUEBA_TEMPLATE_HTML))

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            with self.assertRaises(RealSendTemplateVariablesMissing) as cm:
                process_bulk_id_v2(bulk.pk, job_id=1)

        # missing variable NAMES are safe to assert on (template
        # identifiers, not payload content/PII).
        self.assertIn("nombre", str(cm.exception))
        self.assertIn("cedula", str(cm.exception))
        self.assertIn("codigo", str(cm.exception))
        self.assertIn("Valor", str(cm.exception))
        self.assertIn("Plazo", str(cm.exception))

        # recipient untouched: no intento existio.
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_NOT_STARTED)
        self.assertEqual(row.send_attempt_number, 0)
        self.assertIsNone(row.send_started_at)
        self.assertIsNone(row.sent_at)
        # explicit, not merely implied by the equality above: never the
        # two Doppler-attempt terminal states.
        self.assertNotEqual(row.send_status, BulkSendRecipient.SEND_AMBIGUOUS)
        self.assertNotEqual(row.send_status, BulkSendRecipient.SEND_FAILED)

        # zero send attempt made — only the template-discovery GET fired.
        self._send_mock.assert_not_called()
        self.assertEqual(self._template_mock.call_count, 1)

    # --- item 8: exact compatible payload, positive path --------------------

    def test_compatible_payload_allows_exactly_one_doppler_send(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk, payload=dict(COMPATIBLE_PAYLOAD))
        self.mock_template_transport(return_value=_template_response(PRUEBA_TEMPLATE_HTML))
        self.mock_transport(return_value=FakeDopplerResponse())

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            result = process_bulk_id_v2(bulk.pk, job_id=1)

        self.assertIn("1 fila", result)
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENT)
        self.assertEqual(row.send_attempt_number, 1)
        self.assertIsNotNone(row.send_started_at)
        self.assertIsNotNone(row.sent_at)

        # reclamado exactamente una vez: nada mas queda elegible.
        from relay.services.bulk_v2_send_state import claim_next_recipient
        self.assertIsNone(claim_next_recipient(bulk.pk, job_id=2))

        # exactly one send, exactly one template-discovery GET.
        self.assertEqual(self._send_mock.call_count, 1)
        self.assertEqual(self._template_mock.call_count, 1)

    # --- item 9: discovery-failed also blocks send, zero transport ---------

    def test_discovery_failed_blocks_send_with_zero_transport(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk, payload=dict(FIRST_CANARY_PAYLOAD))
        self.mock_template_transport(return_value=FakeDopplerResponse(
            status_code=200, json_data={"id": "tpl-real", "name": "PRUEBA"},
        ))

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            with self.assertRaises(RealSendTemplateDiscoveryFailed):
                process_bulk_id_v2(bulk.pk, job_id=1)

        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_NOT_STARTED)
        self.assertEqual(row.send_attempt_number, 0)
        self.assertIsNone(row.send_started_at)
        self.assertIsNone(row.sent_at)
        self.assertNotEqual(row.send_status, BulkSendRecipient.SEND_AMBIGUOUS)
        self.assertNotEqual(row.send_status, BulkSendRecipient.SEND_FAILED)
        self._send_mock.assert_not_called()

    def test_no_eligible_rows_skips_template_discovery_entirely(self):
        """Zero rows left to send -> zero Doppler traffic of any kind
        (GET or POST) — preserves the pre-existing no-op idempotency
        invariant (scenario 4/8) unchanged by this gate."""
        from relay.services.bulk_v2_send_state import claim_next_recipient, mark_sent

        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk, payload=dict(COMPATIBLE_PAYLOAD))
        claim_next_recipient(bulk.pk, job_id=0)
        mark_sent(row.pk, message_id="msg-preexisting", location="")

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            result = process_bulk_id_v2(bulk.pk, job_id=1)

        self.assertIn("0 fila", result)
        self._transport_mock.assert_not_called()

    # --- item 5/6: BackgroundJob terminal-error handling --------------------

    def test_variables_missing_leaves_backgroundjob_in_terminal_error_state(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk, payload=dict(FIRST_CANARY_PAYLOAD))
        self.mock_template_transport(return_value=_template_response(PRUEBA_TEMPLATE_HTML))

        job = BackgroundJob.objects.create(
            job_type=BackgroundJob.TYPE_BULK_SEND_V2_REAL,
            bulk=bulk,
            state=BackgroundJob.STATE_QUEUED,
        )

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            run_background_job(job.id)

        job.refresh_from_db()
        self.assertEqual(job.state, BackgroundJob.STATE_ERROR)
        self.assertIn("Variables requeridas ausentes", job.message)
        self.assertNotIn("REALCANARY01", job.message)  # never a payload value

        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_NOT_STARTED)
        self.assertEqual(row.send_attempt_number, 0)
        self.assertIsNone(row.send_started_at)
        self.assertNotEqual(row.send_status, BulkSendRecipient.SEND_AMBIGUOUS)
        self.assertNotEqual(row.send_status, BulkSendRecipient.SEND_FAILED)
        self._send_mock.assert_not_called()

    def test_discovery_failed_leaves_backgroundjob_in_terminal_error_state(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk, payload=dict(FIRST_CANARY_PAYLOAD))
        self.mock_template_transport(return_value=FakeDopplerResponse(
            status_code=200, json_data={"id": "tpl-real", "name": "PRUEBA"},
        ))

        job = BackgroundJob.objects.create(
            job_type=BackgroundJob.TYPE_BULK_SEND_V2_REAL,
            bulk=bulk,
            state=BackgroundJob.STATE_QUEUED,
        )

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            run_background_job(job.id)

        job.refresh_from_db()
        self.assertEqual(job.state, BackgroundJob.STATE_ERROR)
        self.assertIn("determinar las variables requeridas", job.message)

        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_NOT_STARTED)
        self.assertEqual(row.send_attempt_number, 0)
        self.assertIsNone(row.send_started_at)
        self.assertNotEqual(row.send_status, BulkSendRecipient.SEND_AMBIGUOUS)
        self.assertNotEqual(row.send_status, BulkSendRecipient.SEND_FAILED)
        self._send_mock.assert_not_called()

    # --- item 10: rejection never blocks a later, corrected retry ----------

    def test_rejection_does_not_block_retry_after_payload_is_fixed(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk, payload=dict(FIRST_CANARY_PAYLOAD))
        self.mock_template_transport(return_value=_template_response(PRUEBA_TEMPLATE_HTML))

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            with self.assertRaises(RealSendTemplateVariablesMissing):
                process_bulk_id_v2(bulk.pk, job_id=1)

            row.refresh_from_db()
            self.assertEqual(row.send_status, BulkSendRecipient.SEND_NOT_STARTED)

            # operator fixes the payload (out of band, e.g. a corrected
            # re-import in a real operational flow) and retries.
            BulkSendRecipient.objects.filter(pk=row.pk).update(
                payload=dict(COMPATIBLE_PAYLOAD)
            )
            self.mock_transport(return_value=FakeDopplerResponse())
            result = process_bulk_id_v2(bulk.pk, job_id=2)

        self.assertIn("1 fila", result)
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENT)
        self.assertEqual(row.send_attempt_number, 1)


class TemplateVariableGateCommandIntegrationTests(RealSendFixtureMixin, NoRealDopplerCallTestCase):
    """Same two outcomes, exercised through the full management-command
    path rather than calling `process_bulk_id_v2` directly.

    `run_claimed_job` (jobs.py) absorbs the gate's exception internally and
    sets `BackgroundJob.state = STATE_ERROR` — the command itself does NOT
    raise `CommandError` for this outcome (it only raises `CommandError`
    for the 14 upstream authorization checks, checks 2-13; once `handle()`
    reaches `run_claimed_job(claimed)` at check 14, it always reports
    whatever terminal state resulted and returns normally — confirmed by
    reading `bulk_v2_real_send.py`'s `handle()` directly, not assumed)."""

    def test_command_surfaces_variables_missing_via_job_state_not_command_error(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk, payload=dict(FIRST_CANARY_PAYLOAD))
        self.mock_template_transport(return_value=_template_response(PRUEBA_TEMPLATE_HTML))

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            call_command("bulk_v2_real_send", bulk_send_id=bulk.pk)

        job = BackgroundJob.objects.filter(
            bulk=bulk, job_type=BackgroundJob.TYPE_BULK_SEND_V2_REAL
        ).get()
        self.assertEqual(job.state, BackgroundJob.STATE_ERROR)
        self.assertIn("Variables requeridas ausentes", job.message)

        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_NOT_STARTED)
        self._send_mock.assert_not_called()


class CasePreservingImportGateIntegrationTests(RealSendFixtureMixin, NoRealDopplerCallTestCase):
    """fix-bulk-v2-case-preserving-import: end-to-end proof that a REAL
    `BulkImportService` import (not a hand-built `payload={...}` literal)
    of the exact compatible CSV persists case-preserved keys and passes
    the template gate — with zero manual DB manipulation anywhere in the
    chain from CSV bytes to `sent`."""

    def test_real_import_of_compatible_csv_persists_exact_case_and_passes_gate(self):
        user = self.make_user()
        bulk = BulkSend.objects.create(
            engine_version=BulkSend.ENGINE_V2,
            client_request_id="case-preserving-integration-test",
            template_id="tpl-real",
            template_name="PRUEBA",
            scheduled_by=user,
        )
        csv_bytes = (
            "email,nombre,cedula,codigo,Valor,Plazo\r\n"
            "kike@example.com,Canary V2,0999999999,REALCANARY02,10.00,1\r\n"
        ).encode("utf-8")
        bulk.recipients_file.save("compatible.csv", ContentFile(csv_bytes), save=True)
        result = BulkImportService(bulk, import_version=1).import_file()

        self.assertEqual(result.import_status, "ready")
        self.assertEqual((result.total_rows, result.valid_rows, result.invalid_rows), (1, 1, 0))

        row = bulk.recipient_occurrences.get()
        self.assertEqual(row.status, BulkSendRecipient.STATUS_PENDING)
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_NOT_STARTED)
        self.assertEqual(row.send_attempt_number, 0)
        self.assertEqual(
            sorted(row.payload.keys()),
            sorted(["nombre", "cedula", "codigo", "Valor", "Plazo"]),
        )

        self.mock_template_transport(return_value=_template_response(PRUEBA_TEMPLATE_HTML))
        self.mock_transport(return_value=FakeDopplerResponse())

        with override_settings(**self.authorized_settings(user=user, bulk=bulk, domain="example.com")):
            gate_result = process_bulk_id_v2(bulk.pk, job_id=1)

        self.assertIn("1 fila", gate_result)
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENT)
        self.assertEqual(row.send_attempt_number, 1)
        self.assertEqual(self._send_mock.call_count, 1)
