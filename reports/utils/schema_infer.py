from __future__ import annotations

import csv
import json
import re
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


NULL_TOKENS = {"", "null", "none", "n/a", "na", "-", "–"}


@dataclass
class ColumnStat:
    name: str
    non_null: int = 0
    nulls: int = 0
    samples: List[str] = None
    inferred_type: str = "text"

    def to_dict(self):
        d = asdict(self)
        return d


def _is_int(s: str) -> bool:
    try:
        int(s)
        return True
    except Exception:
        return False


def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except Exception:
        return False


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _is_email(s: str) -> bool:
    return bool(EMAIL_RE.match(s))


def _is_bool(s: str) -> bool:
    return s.lower() in {"true", "false", "1", "0", "yes", "no"}


def _is_datetime(s: str) -> bool:
    # try common formats
    for fmt in [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
    ]:
        try:
            datetime.strptime(s, fmt)
            return True
        except Exception:
            continue
    return False


def _infer_type(samples: List[str]) -> str:
    # order: email > int > float > bool > datetime > text
    non_null = [s for s in samples if s is not None and str(s).strip().lower() not in NULL_TOKENS]
    if not non_null:
        return "text"
    if all(_is_email(s) for s in non_null):
        return "email"
    if all(_is_int(s) for s in non_null):
        return "integer"
    if all(_is_float(s) for s in non_null):
        return "float"
    if all(_is_bool(s) for s in non_null):
        return "boolean"
    if all(_is_datetime(s) for s in non_null):
        # prefer datetime over date differentiation for ahora
        return "timestamp"
    return "text"


def infer_csv_schema(path: Path, sample_limit: int = 200) -> Dict:
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = list(reader.fieldnames or [])
        stats: Dict[str, ColumnStat] = {h: ColumnStat(name=h, samples=[]) for h in headers}
        total = 0
        for row in reader:
            total += 1
            for h in headers:
                v = row.get(h)
                token = ("" if v is None else str(v)).strip()
                if token.lower() in NULL_TOKENS:
                    stats[h].nulls += 1
                else:
                    stats[h].non_null += 1
                    if len(stats[h].samples) < 5:
                        stats[h].samples.append(token)
            if total >= sample_limit:
                break

        for h in headers:
            all_samples: List[str] = stats[h].samples.copy()
            stats[h].inferred_type = _infer_type(all_samples)

        return {
            "columns": [stats[h].to_dict() for h in headers],
            "rows_scanned": total,
        }


def save_schema_json(schema: Dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(schema, fh, ensure_ascii=False, indent=2)

