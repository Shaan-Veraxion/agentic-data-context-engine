"""Privacy guard: in the starting stage no route may receive row-level data.

Models may see script text, BI IR, schemas, view SQL, aggregate differences and report layouts. The guard rejects
payloads that look like rows: lists of records, tabular text, or keys that name sample data.
"""
from __future__ import annotations

from typing import Any

ROW_KEYS = {"rows", "records", "sample", "samples", "sample_values", "data_rows", "values_sample", "preview"}
MAX_RECORDS = 3  # a short list of dicts is allowed (e.g. a few aggregate differences); more looks like rows
MAX_TABULAR_LINES = 20


class PrivacyViolation(ValueError):
    pass


def check(payload: Any, path: str = "$") -> None:
    if isinstance(payload, dict):
        for k, v in payload.items():
            if str(k).lower() in ROW_KEYS:
                raise PrivacyViolation(f"{path}.{k}: row-level data is not allowed on this route")
            check(v, f"{path}.{k}")
    elif isinstance(payload, (list, tuple)):
        records = [x for x in payload if isinstance(x, (dict, list, tuple))]
        if len(records) > MAX_RECORDS and _uniform(records):
            raise PrivacyViolation(f"{path}: {len(records)} uniform records look like rows")
        for i, x in enumerate(payload):
            check(x, f"{path}[{i}]")
    elif isinstance(payload, str) and _tabular(payload):
        raise PrivacyViolation(f"{path}: text looks like a delimited data extract")


def _uniform(records: list[Any]) -> bool:
    if all(isinstance(r, dict) for r in records):
        keys = {frozenset(r) for r in records}
        return len(keys) == 1 and not {"field", "name", "kind"} & set(next(iter(keys)))  # schema lists are fine
    return all(isinstance(r, (list, tuple)) and len(r) == len(records[0]) for r in records)


def _tabular(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < MAX_TABULAR_LINES:
        return False
    for sep in (",", ";", "\t", "|"):
        counts = [ln.count(sep) for ln in lines]
        if counts[0] >= 2 and sum(1 for c in counts if c == counts[0]) >= 0.9 * len(lines):
            return True
    return False
