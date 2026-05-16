import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List


def _flatten_value(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=True)
    if value is None:
        return ""
    return str(value)


def write_sqlite(path: Path, records: List[Dict[str, Any]], table: str = "records") -> None:
    if not records:
        return
    if path.exists():
        path.unlink()

    columns = list(records[0].keys())
    placeholders = ", ".join("?" for _ in columns)
    column_defs = ", ".join(f"{col} TEXT" for col in columns)

    conn = sqlite3.connect(path)
    try:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({column_defs})")
        for record in records:
            row = [_flatten_value(record.get(col)) for col in columns]
            conn.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                row,
            )
        conn.commit()
    finally:
        conn.close()
