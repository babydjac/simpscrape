import json
from pathlib import Path
from typing import Any, Dict, List


def write_json(path: Path, records: List[Dict[str, Any]]) -> None:
    path.write_text(json.dumps(records, indent=2, ensure_ascii=True))
