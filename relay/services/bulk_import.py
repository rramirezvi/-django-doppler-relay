from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import os
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from relay.models import BulkSend, BulkSendRecipient


EMAIL_COLUMNS = {
    "email",
    "correo",
    "e-mail",
    "mail",
    "email_address",
    "correo_electronico",
}
BULK_RECIPIENT_NAMESPACE = uuid.UUID("ff03ea1c-8dc4-5e94-bcf8-c6c72b3b7ba1")
SPOOL_FORMAT_VERSION = 1
SPOOL_PREFIX = "bulk-import-"
SPOOL_SUFFIX = ".jsonl"
logger = logging.getLogger(__name__)


class BulkImportError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BulkImportResult:
    bulk_send_id: int
    import_version: int
    import_status: str
    total_rows: int
    valid_rows: int
    invalid_rows: int


def _normalize_number(value: int | float | Decimal) -> int | float:
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and not math.isfinite(value):
        raise BulkImportError(
            "payload_not_serializable",
            "El payload contiene un numero no finito.",
        )
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BulkImportError(
            "payload_not_serializable",
            "El payload contiene un numero invalido.",
        ) from exc
    if not decimal_value.is_finite():
        raise BulkImportError(
            "payload_not_serializable",
            "El payload contiene un numero no finito.",
        )
    normalized = decimal_value.normalize()
    if normalized == normalized.to_integral_value():
        return int(normalized)
    float_value = float(normalized)
    if (
        not math.isfinite(float_value)
        or Decimal(str(float_value)).normalize() != normalized
    ):
        raise BulkImportError(
            "payload_number_precision_unsupported",
            "El payload contiene un decimal que no puede representarse sin perdida.",
        )
    # json.dumps uses Python's stable shortest-round-trip float representation.
    return float_value


def normalize_payload(value: Any) -> Any:
    """
    Normalize logical JSON values before hashing.

    Strings and keys use Unicode NFC, dictionary keys are sorted, list order is
    retained, finite numbers are normalized through Decimal, and operational
    keys prefixed with ``__`` are excluded.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, (int, float, Decimal)):
        return _normalize_number(value)
    if isinstance(value, list):
        return [normalize_payload(item) for item in value]
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise BulkImportError(
                    "payload_not_serializable",
                    "El payload contiene una clave que no es texto.",
                )
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key.startswith("__"):
                continue
            normalized[normalized_key] = normalize_payload(item)
        return {key: normalized[key] for key in sorted(normalized)}
    raise BulkImportError(
        "payload_not_serializable",
        "El payload contiene un tipo no serializable.",
    )


def canonicalize_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bytes, str]:
    """Hash only the canonical payload, never recipient or occurrence identity."""
    normalized = normalize_payload(payload)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return normalized, encoded, hashlib.sha256(encoded).hexdigest()


def occurrence_idempotency_key(
    *,
    bulk_send_id: int,
    import_version: int,
    source_row_number: int,
    payload_hash: str,
) -> uuid.UUID:
    identity = (
        f"{bulk_send_id}:{import_version}:{source_row_number}:{payload_hash}"
    )
    return uuid.uuid5(BULK_RECIPIENT_NAMESPACE, identity)


def _normalized_header(value: str) -> str:
    """Matching-only form: NFC + strip + casefold. Never persisted as a
    payload/variable key — used exclusively to detect the email column,
    to look up `csv_column` values inside `clean`, and as the collision
    key in `_build_header_index`/`_build_variable_key_index`."""
    return unicodedata.normalize("NFC", str(value or "")).strip().casefold()


def _case_preserving_header(value: str) -> str:
    """Persisted form: NFC + strip, WITHOUT casefold. This is what ends
    up as a literal key in `BulkSendRecipient.payload` (and, via the
    variables mapping, as the literal Mustache variable name sent to
    Doppler) — case-preserving-import (fix-bulk-v2-case-preserving-import)
    exists specifically so a CSV column named "Valor" persists as
    "Valor", not "valor"."""
    return unicodedata.normalize("NFC", str(value or "")).strip()


def _build_header_index(fieldnames: list[str | None]) -> dict[str, str]:
    """normalized -> case-preserving, one entry per distinct CSV column.

    Strict by design (fix-bulk-v2-case-preserving-import): ANY second
    header whose `_normalized_header()` already exists in the index
    raises `duplicate_header`, regardless of whether its case-preserving
    form is identical, differs only in case, only in whitespace, or only
    in Unicode composition. Two physically distinct CSV columns are never
    silently collapsed into one payload key — if both columns exist in
    the file, the file is rejected outright. This applies uniformly to
    `Valor,valor` / `Valor,Valor` / `Valor," Valor "` / two NFC-equivalent
    byte-different headers / any other pair colliding after
    NFC+strip+casefold."""
    index: dict[str, str] = {}
    for raw in fieldnames:
        if raw is None:
            continue
        normalized = _normalized_header(raw)
        preserved = _case_preserving_header(raw)
        if normalized in index:
            raise BulkImportError(
                "duplicate_header",
                "Encabezados ambiguos tras normalizar: "
                f"'{index[normalized]}' y '{preserved}' representan la misma columna.",
            )
        index[normalized] = preserved
    return index


def _build_variable_key_index(raw_mapping: dict[Any, Any]) -> dict[str, str]:
    """Same strict collision policy as `_build_header_index`, applied to
    `BulkSend.variables`' keys (the Doppler template-variable names an
    operator configures, independent of CSV header text). Returns
    normalized -> case-preserving `template_key`; raises
    `duplicate_variable_mapping_key` on any collision."""
    index: dict[str, str] = {}
    for template_key in raw_mapping:
        if not isinstance(template_key, str) or template_key.startswith("__"):
            continue
        normalized = _normalized_header(template_key)
        preserved = _case_preserving_header(template_key)
        if normalized in index:
            raise BulkImportError(
                "duplicate_variable_mapping_key",
                "Claves de variables ambiguas tras normalizar: "
                f"'{index[normalized]}' y '{preserved}' representan la misma variable.",
            )
        index[normalized] = preserved
    return index


def _normalize_recipient(value: str) -> str:
    recipient = unicodedata.normalize("NFC", str(value or "")).strip()
    if "@" not in recipient:
        return recipient
    local, domain = recipient.rsplit("@", 1)
    return f"{local}@{domain.casefold()}"


def _sanitize_error(message: str) -> str:
    return " ".join(str(message).split())[:255]


def cleanup_stale_spools() -> int:
    """Remove only stale, regular TD-02A spools from the system temp directory."""
    max_age = max(
        3600,
        int(
            getattr(
                settings,
                "BULK_PROCESSING_V2_SPOOL_MAX_AGE_SECONDS",
                24 * 60 * 60,
            )
        ),
    )
    cutoff = time.time() - max_age
    removed = 0
    temp_root = Path(tempfile.gettempdir()).resolve()
    for candidate in temp_root.glob(f"{SPOOL_PREFIX}*{SPOOL_SUFFIX}"):
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if resolved.parent != temp_root or resolved.stat().st_mtime >= cutoff:
                continue
            resolved.unlink()
            removed += 1
        except OSError:
            continue
    return removed


class BulkImportService:
    """
    Import a CSV into an occurrence ledger.

    Preflight writes private UTF-8 JSON Lines. The first record identifies the
    spool format/version; every later record is one complete occurrence DTO.
    JSONL supports streaming and bounded memory. It consumes more disk than a
    binary format, but is deterministic, inspectable and dependency-free. The
    temporary file is removed after success or failure.
    """

    def __init__(
        self,
        bulk_send: BulkSend,
        *,
        import_version: int = 1,
        allow_new_version: bool = False,
    ):
        self.bulk_send = bulk_send
        self.import_version = import_version
        self.allow_new_version = allow_new_version
        self.max_bytes = int(
            getattr(settings, "BULK_PROCESSING_V2_MAX_FILE_BYTES", 50 * 1024 * 1024)
        )
        self.max_rows = int(
            getattr(settings, "BULK_PROCESSING_V2_MAX_ROWS", 250_000)
        )
        self.batch_size = max(
            1, int(getattr(settings, "BULK_PROCESSING_V2_IMPORT_BATCH_SIZE", 1000))
        )

    def import_file(self) -> BulkImportResult:
        if self.bulk_send.engine_version != BulkSend.ENGINE_V2:
            raise BulkImportError(
                "engine_invalid", "La importacion persistente requiere engine v2."
            )
        if self.import_version < 1:
            raise BulkImportError(
                "import_version_invalid", "import_version debe ser mayor que cero."
            )

        spool_path: Path | None = None
        try:
            spool_path, total, valid, invalid = self._build_spool()
            spool_bytes = spool_path.stat().st_size
            logger.info(
                "bulk_v2_spool bulk_id=%s rows=%s bytes=%s mode=0600",
                self.bulk_send.pk,
                total,
                spool_bytes,
            )
            result = self._persist_spool(
                spool_path,
                total_rows=total,
                valid_rows=valid,
                invalid_rows=invalid,
            )
            logger.info(
                "bulk_v2_spool bulk_id=%s result=%s ledger=%s",
                self.bulk_send.pk,
                result.import_status,
                result.total_rows,
            )
            return result
        except BulkImportError as exc:
            self._record_import_error(exc)
            raise
        except Exception as exc:
            error = BulkImportError(
                "internal_import_error",
                "La importacion no pudo completarse.",
            )
            self._record_import_error(error)
            raise error from exc
        finally:
            if spool_path:
                try:
                    spool_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _build_spool(self) -> tuple[Path, int, int, int]:
        cleanup_stale_spools()
        upload_size = getattr(self.bulk_send.recipients_file, "size", None)
        if upload_size is not None and upload_size > self.max_bytes:
            raise BulkImportError(
                "file_too_large",
                f"El CSV excede el limite de {self.max_bytes} bytes.",
            )

        spool = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=SPOOL_PREFIX,
            suffix=SPOOL_SUFFIX,
            delete=False,
        )
        spool_path = Path(spool.name)
        try:
            try:
                os.chmod(spool_path, 0o600)
            except OSError:
                pass
            spool.write(
                json.dumps(
                    {"format": "bulk-recipient-jsonl", "version": SPOOL_FORMAT_VERSION},
                    separators=(",", ":"),
                )
                + "\n"
            )
            total = valid = invalid = 0
            for dto in self._iter_rows():
                total += 1
                if total > self.max_rows:
                    raise BulkImportError(
                        "row_limit_exceeded",
                        f"El CSV excede el limite de {self.max_rows} filas.",
                    )
                if dto["status"] == BulkSendRecipient.STATUS_PENDING:
                    valid += 1
                else:
                    invalid += 1
                spool.write(
                    json.dumps(
                        dto,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            if total == 0:
                raise BulkImportError("file_empty", "El CSV no contiene filas de datos.")
            return spool_path, total, valid, invalid
        except Exception:
            spool.close()
            spool_path.unlink(missing_ok=True)
            raise
        finally:
            if not spool.closed:
                spool.close()

    def _iter_rows(self) -> Iterator[dict[str, Any]]:
        try:
            source = self.bulk_send.recipients_file.open("rb")
        except OSError as exc:
            raise BulkImportError("file_unreadable", "No se pudo leer el CSV.") from exc
        text_stream = io.TextIOWrapper(
            source,
            encoding="utf-8-sig",
            errors="strict",
            newline="",
        )
        try:
            sample = text_stream.read(4096)
            if not sample:
                raise BulkImportError("file_empty", "El CSV esta vacio.")
            text_stream.seek(0)
            try:
                delimiter = csv.Sniffer().sniff(sample, delimiters=",;").delimiter
            except csv.Error:
                delimiter = ";" if sample.count(";") > sample.count(",") else ","

            reader = csv.DictReader(text_stream, delimiter=delimiter)
            if not reader.fieldnames:
                raise BulkImportError("header_missing", "El CSV no tiene cabecera.")
            header_index = _build_header_index(reader.fieldnames)
            email_column = next(
                (normalized for normalized in header_index if normalized in EMAIL_COLUMNS),
                None,
            )
            if not email_column:
                raise BulkImportError(
                    "email_column_missing",
                    "El CSV no contiene una columna de correo.",
                )

            raw_variables = (
                self.bulk_send.variables
                if isinstance(self.bulk_send.variables, dict)
                else {}
            )
            variable_key_index = _build_variable_key_index(raw_variables)
            mapping = {
                variable_key_index[_normalized_header(template_key)]: _normalized_header(csv_column)
                for template_key, csv_column in raw_variables.items()
                if isinstance(template_key, str)
                and isinstance(csv_column, str)
                and not template_key.startswith("__")
            }

            for row_number, row in enumerate(reader, start=1):
                yield self._parse_row(
                    row=row,
                    source_row_number=row_number,
                    email_column=email_column,
                    mapping=mapping,
                    header_index=header_index,
                )
        except UnicodeDecodeError as exc:
            raise BulkImportError(
                "encoding_invalid", "El CSV debe estar codificado en UTF-8."
            ) from exc
        finally:
            text_stream.close()

    def _parse_row(
        self,
        *,
        row: dict[str | None, str | list[str] | None],
        source_row_number: int,
        email_column: str,
        mapping: dict[str, str],
        header_index: dict[str, str],
    ) -> dict[str, Any]:
        structural_error = None in row
        clean = {
            _normalized_header(key or ""): (
                ""
                if value is None
                else unicodedata.normalize("NFC", str(value)).strip()
            )
            for key, value in row.items()
            if key is not None
        }
        recipient = clean.get(email_column, "")
        normalized_recipient = _normalize_recipient(recipient)

        if mapping:
            # `mapping`'s keys are already the case-preserving template_key
            # (built in _iter_rows via variable_key_index) — persisted
            # verbatim, no further transformation here.
            payload = {
                template_key: clean.get(csv_column)
                for template_key, csv_column in mapping.items()
            }
            missing_required = [
                key for key, value in payload.items() if value is None
            ]
        else:
            # header_index maps the same normalized key `clean` uses back
            # to its case-preserving original — this is the one place the
            # persisted payload key is chosen (fix-bulk-v2-case-preserving-import).
            payload = {
                header_index[key]: value
                for key, value in clean.items()
                if key != email_column and not key.startswith("__")
            }
            missing_required = []

        error_code = ""
        error_message = ""
        if structural_error:
            error_code = "row_structure_invalid"
            error_message = "La fila contiene mas valores que la cabecera."
        elif not recipient:
            error_code = "recipient_empty"
            error_message = "La fila no contiene destinatario."
        else:
            try:
                validate_email(recipient)
            except ValidationError:
                error_code = "recipient_invalid"
                error_message = "El destinatario no tiene un formato valido."
        if not error_code and missing_required:
            error_code = "required_value_missing"
            error_message = "Falta una columna requerida para la personalizacion."

        try:
            normalized_payload, _, payload_hash = canonicalize_payload(payload)
        except BulkImportError as exc:
            normalized_payload = {}
            _, _, payload_hash = canonicalize_payload({})
            error_code = error_code or exc.code
            error_message = error_message or str(exc)

        status = (
            BulkSendRecipient.STATUS_INVALID
            if error_code
            else BulkSendRecipient.STATUS_PENDING
        )
        idempotency_key = occurrence_idempotency_key(
            bulk_send_id=self.bulk_send.pk,
            import_version=self.import_version,
            source_row_number=source_row_number,
            payload_hash=payload_hash,
        )
        return {
            "import_version": self.import_version,
            "source_row_number": source_row_number,
            "recipient": recipient,
            "normalized_recipient": normalized_recipient,
            "payload": normalized_payload,
            "payload_hash": payload_hash,
            "idempotency_key": str(idempotency_key),
            "status": status,
            "last_error_code": error_code,
            "last_error_message": _sanitize_error(error_message),
        }

    def _spool_rows(self, spool_path: Path) -> Iterator[dict[str, Any]]:
        with spool_path.open("r", encoding="utf-8") as spool:
            header = json.loads(spool.readline())
            if header != {
                "format": "bulk-recipient-jsonl",
                "version": SPOOL_FORMAT_VERSION,
            }:
                raise BulkImportError(
                    "spool_invalid", "El spool de importacion no es valido."
                )
            for line in spool:
                yield json.loads(line)

    def _persist_spool(
        self,
        spool_path: Path,
        *,
        total_rows: int,
        valid_rows: int,
        invalid_rows: int,
    ) -> BulkImportResult:
        with transaction.atomic():
            bulk = BulkSend.objects.select_for_update().get(pk=self.bulk_send.pk)
            if bulk.engine_version != BulkSend.ENGINE_V2:
                raise BulkImportError(
                    "engine_changed", "La campana ya no utiliza engine v2."
                )
            first_import = (
                bulk.import_status
                in {BulkSend.IMPORT_NOT_STARTED, BulkSend.IMPORT_ERROR}
                and bulk.import_version == 0
                and not bulk.recipient_occurrences.exists()
            )
            explicit_next_version = (
                self.allow_new_version
                and bulk.import_status
                in {
                    BulkSend.IMPORT_READY,
                    BulkSend.IMPORT_READY_WITH_ERRORS,
                }
                and self.import_version == bulk.import_version + 1
            )
            if not (first_import or explicit_next_version):
                raise BulkImportError(
                    "import_already_started",
                    "La importacion de esta campana ya fue iniciada.",
                )
            if bulk.recipient_occurrences.filter(
                import_version=self.import_version
            ).exists():
                raise BulkImportError(
                    "import_version_exists",
                    "La version de importacion ya existe.",
                )

            now = timezone.now()
            bulk.import_status = BulkSend.IMPORT_IMPORTING
            bulk.import_started_at = now
            bulk.import_error = ""
            bulk.import_version = self.import_version
            bulk.save(
                update_fields=[
                    "import_status",
                    "import_started_at",
                    "import_error",
                    "import_version",
                ]
            )

            batch: list[BulkSendRecipient] = []
            for dto in self._spool_rows(spool_path):
                batch.append(BulkSendRecipient(bulk_send=bulk, **dto))
                if len(batch) >= self.batch_size:
                    BulkSendRecipient.objects.bulk_create(
                        batch, batch_size=self.batch_size
                    )
                    batch.clear()
            if batch:
                BulkSendRecipient.objects.bulk_create(
                    batch, batch_size=self.batch_size
                )

            bulk.imported_rows = total_rows
            bulk.valid_rows = valid_rows
            bulk.invalid_rows = invalid_rows
            bulk.import_finished_at = timezone.now()
            bulk.import_status = (
                BulkSend.IMPORT_READY_WITH_ERRORS
                if invalid_rows
                else BulkSend.IMPORT_READY
            )
            bulk.save(
                update_fields=[
                    "imported_rows",
                    "valid_rows",
                    "invalid_rows",
                    "import_finished_at",
                    "import_status",
                ]
            )

        self.bulk_send.refresh_from_db()
        return BulkImportResult(
            bulk_send_id=self.bulk_send.pk,
            import_version=self.import_version,
            import_status=self.bulk_send.import_status,
            total_rows=total_rows,
            valid_rows=valid_rows,
            invalid_rows=invalid_rows,
        )

    def _record_import_error(self, error: BulkImportError) -> None:
        BulkSend.objects.filter(
            pk=self.bulk_send.pk,
            engine_version=BulkSend.ENGINE_V2,
            import_status__in=[
                BulkSend.IMPORT_NOT_STARTED,
                BulkSend.IMPORT_ERROR,
            ],
            import_version=0,
        ).update(
            import_status=BulkSend.IMPORT_ERROR,
            import_finished_at=timezone.now(),
            import_error=_sanitize_error(str(error)),
        )
        self.bulk_send.refresh_from_db()
