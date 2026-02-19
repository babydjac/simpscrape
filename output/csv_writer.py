import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def _flatten_value(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=True)
    if value is None:
        return ""
    return str(value)


def write_csv(path: Path, records: List[Dict[str, Any]], fieldnames: Optional[List[str]] = None) -> None:
    if not records:
        path.write_text("")
        return

    if fieldnames is None:
        fieldnames = list(records[0].keys())
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            row = {key: _flatten_value(record.get(key)) for key in fieldnames}
            writer.writerow(row)
