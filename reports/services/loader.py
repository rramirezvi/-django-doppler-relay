from __future__ import annotations

import csv
import re
from pathlib import Path
from zoneinfo import ZoneInfo
from django.conf import settings
from typing import Dict, Iterable, List, Tuple

from django.db import connections
from django.utils import timezone

from reports.models import GeneratedReport


def _sanitize_identifier(name: str) -> str:
    s = re.sub(r"\s+", "_", str(name or "").strip())
    s = re.sub(r"[^0-9a-zA-Z_]+", "_", s)
    s = re.sub(r"_+", "_", s)
    if not s:
        s = "col"
    if s[0].isdigit():
        s = f"c_{s}"
    return s.lower()


def _placeholder_for(vendor: str) -> str:
    return "?" if vendor == "sqlite" else "%s"


def _table_name_for(report_type: str) -> str:
    return f"reports_{report_type.strip().lower()}"


def _existing_columns_sql(vendor: str, table: str) -> str:
    if vendor == "sqlite":
        return f"PRAGMA table_info({table});"
    # Postgres / others via information_schema
    return (
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s"
    )


def _sql_type_for(vendor: str, inferred: str) -> str:
    inferred = (inferred or "text").lower()
    if vendor == "sqlite":
        return {
            "integer": "INTEGER",
            "float": "REAL",
            "boolean": "INTEGER",
            "timestamp": "TIMESTAMP",
            "email": "TEXT",
            "text": "TEXT",
        }.get(inferred, "TEXT")
    # default (postgres, mysql)
    return {
        "integer": "INTEGER",
        "float": "DOUBLE PRECISION",
        "boolean": "BOOLEAN",
        "timestamp": "TIMESTAMP WITH TIME ZONE",
        "email": "TEXT",
        "text": "TEXT",
    }.get(inferred, "TEXT")


def _ensure_table(connection, table: str, columns_types: List[Tuple[str, str]]) -> None:
    vendor = connection.vendor
    qn = connection.ops.quote_name
    with connection.cursor() as cursor:
        # Create table if not exists with base columns
        cols_def = ", ".join([f"{qn(c)} {t}" for c, t in columns_types] + [
            f"{qn('generated_report_id')} INTEGER",
            f"{qn('created_at')} TIMESTAMP",
        ])
        if vendor == "sqlite":
            cursor.execute(f"CREATE TABLE IF NOT EXISTS {qn(table)} ({cols_def})")
        else:
            cursor.execute(f"CREATE TABLE IF NOT EXISTS {qn(table)} ({cols_def})")

        # Ensure missing columns are added (if schema cambió)
        try:
            if vendor == "sqlite":
                cursor.execute(_existing_columns_sql(vendor, qn(table)))
                existing = {row[1].lower() for row in cursor.fetchall()}  # row[1] is name
            else:
                cursor.execute(_existing_columns_sql(vendor, table), [table])
                existing = {row[0].lower() for row in cursor.fetchall()}
        except Exception:
            existing = set()

        needed = [(c, t) for c, t in columns_types if c.lower() not in existing]
        for col, typ in needed:
            try:
                cursor.execute(f"ALTER TABLE {qn(table)} ADD COLUMN {qn(col)} {typ}")
            except Exception:
                pass  # tolerar si no soporta IF NOT EXISTS y ya existe


def _read_csv(path: Path) -> Tuple[List[str], List[Dict[str, str]], str]:
    """Lee un CSV probando múltiples codificaciones.
    Devuelve (headers, rows, encoding_usada).
    """
    encodings = ["utf-8-sig", "utf-8", "cp1252", "latin-1"]
    last_exc: Exception | None = None
    for enc in encodings:
        try:
            with path.open("r", encoding=enc, newline="") as fh:
                reader = csv.DictReader(fh)
                headers = list(reader.fieldnames or [])
                rows: List[Dict[str, str]] = []
                for row in reader:
                    rows.append({k: ("" if v is None else str(v)) for k, v in row.items()})
                return headers, rows, enc
        except UnicodeDecodeError as exc:
            last_exc = exc
            continue
        except Exception as exc:  # tolerante ante archivos parcialmente corruptos
            last_exc = exc
            continue
    # Si todas fallan, relanzar la última excepción para diagnóstico
    if last_exc:
        raise last_exc
    return [], [], encodings[0]


def to_local_naive(val: str | None) -> str | None:
    """Convierte una fecha/hora en string a hora local sin zona (naive) usando settings.TIME_ZONE.
    Retorna 'YYYY-MM-DD HH:MM:SS' o None si no puede parsear.
    """
    s = ("" if val is None else str(val)).strip()
    if not s:
        return None
    from datetime import datetime
    tz_local = ZoneInfo(getattr(settings, "TIME_ZONE", "UTC"))
    fmts = (
        ("%Y-%m-%d %H:%M:%S", None),
        ("%Y-%m-%dT%H:%M:%S", None),
        ("%Y-%m-%dT%H:%M:%SZ", "UTC"),
        ("%Y-%m-%d", None),
    )
    for fmt, tzname in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            if tzname == "UTC":
                dt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz_local)
            else:
                dt = dt.replace(tzinfo=tz_local)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
    return None


def load_report_to_db(generated_report_id: int, target_alias: str = "default") -> int:
    rep = GeneratedReport.objects.get(pk=generated_report_id)
    if not rep.file_path:
        raise ValueError("El reporte no tiene archivo asociado")

    path = Path(rep.file_path)
    if not path.exists():
        raise FileNotFoundError(f"No existe el archivo: {path}")

    headers, data_rows, used_encoding = _read_csv(path)
    if not headers:
        raise ValueError("El CSV no tiene cabeceras")

    # Detección de "summary" (Subject, Sender, SenderName, Email, Status, Date, Opens, Clicks)
    # Normalizar cabeceras y remover posibles BOM residuales
    def _norm_header(h: str) -> str:
        s = str(h or "").strip().lstrip("\ufeff").lower()
        # limpiar artefactos visibles de BOM si quedaron en texto ya decodificado
        if s.startswith("ï»¿".lower()):
            s = s.replace("ï»¿".lower(), "").strip()
        return s

    headers_lower = [_norm_header(h) for h in headers]
    header_set = set(headers_lower)
    summary_expected = {"subject", "sender", "sendername", "email", "status", "date", "opens", "clicks"}

    columns_types: List[Tuple[str, str]] = []
    cast_types: Dict[str, str] = {}

    is_summary = summary_expected.issubset(header_set)

    if is_summary:
        # Definir esquema tipado estable para summary
        type_map = {
            "subject": "text",
            "sender": "text",
            "sendername": "text",
            "email": "email",
            "status": "text",
            "date": "timestamp",
            "opens": "integer",
            "clicks": "integer",
        }
        for orig, low in zip(headers, headers_lower):
            t = type_map.get(low, "text")
            cast_types[orig] = t
            columns_types.append((_sanitize_identifier(orig), _sql_type_for(connections[target_alias].vendor, t)))
        # Cargar SIEMPRE en la tabla única de resumen operativo
        columns_types.append(("date_local", _sql_type_for(connections[target_alias].vendor, "timestamp_naive")))
        table = "reports_deliveries"
    else:
        # Intentar esquema tipado desde JSON si existe (modo por tipo)
        schema_path = Path("attachments") / "reports" / "schemas" / f"schema_{rep.report_type}.json"
        if schema_path.exists():
            import json
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            cols = schema.get("columns", [])
            inferred_map = {c.get("name"): (c.get("inferred_type") or "text") for c in cols}
            for h in headers:
                inferred = inferred_map.get(h, "text")
                cast_types[h] = inferred
                columns_types.append((_sanitize_identifier(h), _sql_type_for(connections[target_alias].vendor, inferred)))
        else:
            # Fallback: todo TEXT
            for h in headers:
                cast_types[h] = "text"
                columns_types.append((_sanitize_identifier(h), _sql_type_for(connections[target_alias].vendor, "text")))

        table = _table_name_for(rep.report_type)
    connection = connections[target_alias]

    # Asegurar tabla y columnas en destino (tipadas)
    _ensure_table(connection, table, columns_types)

    # Preparar inserción
    vendor = connection.vendor
    qn = connection.ops.quote_name
    ph = _placeholder_for(vendor)

    mapped = [_sanitize_identifier(h) for h in headers]
    extra_cols = ["date_local"] if is_summary else []
    cols_list = mapped + extra_cols + ["generated_report_id", "created_at"]
    cols_sql = ", ".join(qn(c) for c in cols_list)
    placeholders = ", ".join([ph] * len(cols_list))
    insert_sql = f"INSERT INTO {qn(table)} ({cols_sql}) VALUES ({placeholders})"

    # Construir lotes de params
    created_at = timezone.now()
    params_iter: List[Tuple] = []
    NULL_TOKENS = {"", "null", "none", "n/a", "na", "-", "–"}

    from datetime import datetime

    def cast_value(val: str, typ: str):
        s = ("" if val is None else str(val)).strip()
        if s.lower() in NULL_TOKENS:
            return None
        t = (typ or "text").lower()
        if t == "integer":
            try:
                return int(float(s))
            except Exception:
                return None
        if t == "float":
            try:
                return float(s)
            except Exception:
                return None
        if t == "boolean":
            return s.lower() in {"true", "1", "yes"}
        if t == "timestamp":
            tz_local = ZoneInfo(getattr(settings, "TIME_ZONE", "UTC"))
            # intentos de parseo comunes (naive y con 'Z')
            fmts = (
                ("%Y-%m-%d %H:%M:%S", None),
                ("%Y-%m-%dT%H:%M:%S", None),
                ("%Y-%m-%dT%H:%M:%SZ", "UTC"),
                ("%Y-%m-%d", None),
            )
            for fmt, tzname in fmts:
                try:
                    dt = datetime.strptime(s, fmt)
                    if tzname == "UTC":
                        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
                    else:
                        # si no trae zona, asumimos TZ local y convertimos a UTC
                        dt = dt.replace(tzinfo=tz_local)
                    dt_utc = dt.astimezone(ZoneInfo("UTC"))
                    return dt_utc.isoformat(sep=" ")
                except Exception:
                    continue
            # si no parsea, devolver como texto compatible
            return s
        # email y text → string crudo
        return s

    for row in data_rows:
        values = [cast_value(row.get(orig, ""), cast_types.get(orig, "text")) for orig in headers]
        # Añadir date_local cuando es summary (CSV Subject/Sender/...)
        if is_summary:
            try:
                idx = headers_lower.index("date") if "date" in headers_lower else -1
            except Exception:
                idx = -1
            date_local_val = None
            if idx >= 0:
                date_local_val = to_local_naive(row.get(headers[idx]))
            params_iter.append(tuple(values + [date_local_val, rep.pk, created_at]))
        else:
            params_iter.append(tuple(values + [rep.pk, created_at]))

    # Reemplazo por día (ventana local) cuando se trata de deliveries summary
    try:
        if table == 'reports_deliveries' and rep.start_date and rep.end_date and rep.start_date == rep.end_date:
            from datetime import timedelta, datetime as _dt
            start_local = f"{rep.start_date} 00:00:00"
            end_local = f"{(rep.start_date + timedelta(days=1))} 00:00:00"
            with connection.cursor() as cursor:
                # Primero intentamos por date_local (si existe)
                try:
                    cursor.execute(
                        f'DELETE FROM {qn(table)} WHERE {qn("date_local")} >= ' + ph + ' AND ' + qn('date_local') + ' < ' + ph,
                        [start_local, end_local]
                    )
                except Exception:
                    # Fallback: borrar por rango UTC sobre columna "date"
                    tz_local = ZoneInfo(getattr(settings, "TIME_ZONE", "UTC"))
                    sdt = _dt.strptime(start_local, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz_local).astimezone(ZoneInfo("UTC"))
                    edt = _dt.strptime(end_local, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz_local).astimezone(ZoneInfo("UTC"))
                    cursor.execute(
                        f'DELETE FROM {qn(table)} WHERE {qn("date")} >= ' + ph + ' AND ' + qn('date') + ' < ' + ph,
                        [sdt.isoformat(sep=' '), edt.isoformat(sep=' ')]
                    )
    except Exception:
        pass

    # Idempotencia por generated_report_id: eliminar previamente lo cargado
    try:
        with connection.cursor() as cursor:
            cursor.execute(f'DELETE FROM {qn(table)} WHERE {qn("generated_report_id")} = ' + ph, [rep.pk])
    except Exception:
        pass

    rows_inserted = 0
    try:
        with connection.cursor() as cursor:
            # chunked executemany
            CHUNK = 1000
            for i in range(0, len(params_iter), CHUNK):
                chunk = params_iter[i:i+CHUNK]
                cursor.executemany(insert_sql, chunk)
                rows_inserted += len(chunk)
    except Exception as exc:
        # Registrar error en el modelo y relanzar
        rep.error_details = f"Carga a BD fallo ({target_alias}): {exc}"
        rep.save(update_fields=["error_details", "updated_at"])
        raise

    # Log resumen de esquema utilizado
    try:
        log_dir = Path("attachments") / "reports" / "schemas"
        log_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            f"Load report {rep.pk} type={rep.report_type} alias={target_alias}",
            f"Table {table}",
            f"Encoding {used_encoding}",
        ]
        for orig, mapped_name in zip(headers, mapped):
            lines.append(f"  {orig} -> {mapped_name} ({cast_types.get(orig,'text')})")
        (log_dir / f"load_{rep.pk}.log").write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass

    rep.loaded_to_db = True
    rep.loaded_at = timezone.now()
    rep.rows_inserted = int(rows_inserted)
    rep.last_loaded_alias = target_alias
    rep.save(update_fields=["loaded_to_db", "loaded_at", "rows_inserted", "last_loaded_alias", "updated_at"])
    return rows_inserted



