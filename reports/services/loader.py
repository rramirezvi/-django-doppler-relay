from __future__ import annotations

import csv
import re
from pathlib import Path
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


def _read_csv(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = list(reader.fieldnames or [])
        rows = []
        for row in reader:
            rows.append({k: ("" if v is None else str(v)) for k, v in row.items()})
        return headers, rows


def load_report_to_db(generated_report_id: int, target_alias: str = "default") -> int:
    rep = GeneratedReport.objects.get(pk=generated_report_id)
    if not rep.file_path:
        raise ValueError("El reporte no tiene archivo asociado")

    path = Path(rep.file_path)
    if not path.exists():
        raise FileNotFoundError(f"No existe el archivo: {path}")

    headers, data_rows = _read_csv(path)
    if not headers:
        raise ValueError("El CSV no tiene cabeceras")

    # Intentar esquema tipado desde JSON si existe
    schema_path = Path("attachments") / "reports" / "schemas" / f"schema_{rep.report_type}.json"
    columns_types: List[Tuple[str, str]] = []
    cast_types: Dict[str, str] = {}
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
    cols_list = mapped + ["generated_report_id", "created_at"]
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
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(s, fmt)
                    # entregamos string ISO para compatibilidad universal
                    return dt.isoformat(sep=" ")
                except Exception:
                    continue
            return s  # si no parsea, insertamos como texto compatible
        # email y text → string crudo
        return s

    for row in data_rows:
        values = [cast_value(row.get(orig, ""), cast_types.get(orig, "text")) for orig in headers]
        params_iter.append(tuple(values + [rep.pk, created_at]))

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
        lines = [f"Load report {rep.pk} type={rep.report_type} alias={target_alias}", f"Table {table}"]
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
