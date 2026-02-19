from typing import Any, Dict, List


class DataCollector:
    def __init__(self):
        self.records: List[Dict[str, Any]] = []

    def add_records(self, records: List[Dict[str, Any]]):
        for record in records:
            self.records.append(record)

    def all_records(self) -> List[Dict[str, Any]]:
        return self.records
