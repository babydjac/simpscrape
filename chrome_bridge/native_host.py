#!/usr/bin/env python3
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.chrome_bridge import normalize_origin, remove_storage_state, write_bridge_meta, write_storage_state


def _read_message() -> dict[str, Any]:
    raw_length = sys.stdin.buffer.read(4)
    if len(raw_length) != 4:
        raise EOFError("No native message received.")
    message_length = struct.unpack("=I", raw_length)[0]
    payload = sys.stdin.buffer.read(message_length)
    if len(payload) != message_length:
        raise EOFError("Native message body was truncated.")
    parsed = json.loads(payload.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("Native message payload must be a JSON object.")
    return parsed


def _write_message(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("=I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _sync_cookies(message: dict[str, Any]) -> dict[str, Any]:
    origin = normalize_origin(str(message.get("origin") or ""))
    cookies = message.get("cookies")
    if not isinstance(cookies, list):
        raise ValueError("cookies must be a list.")
    output_path = write_storage_state(origin, cookies)
    meta_path = write_bridge_meta(
        origin,
        {
            "state_path": str(output_path),
            "cookie_count": len(cookies),
            "synced_at": str(message.get("syncedAt") or ""),
            "reason": str(message.get("reason") or ""),
        },
    )
    return {
        "ok": True,
        "origin": origin,
        "statePath": str(output_path),
        "metaPath": str(meta_path),
        "cookieCount": len(cookies),
    }


def _remove_origin(message: dict[str, Any]) -> dict[str, Any]:
    origin = normalize_origin(str(message.get("origin") or ""))
    removed = remove_storage_state(origin)
    return {
        "ok": True,
        "origin": origin,
        "removed": bool(removed),
        "statePath": str(removed) if removed else "",
    }


def _handle_message(message: dict[str, Any]) -> dict[str, Any]:
    message_type = str(message.get("type") or "").strip()
    if message_type == "ping":
        return {"ok": True, "host": "simpscrape", "repoRoot": str(REPO_ROOT)}
    if message_type == "syncCookies":
        return _sync_cookies(message)
    if message_type == "removeOrigin":
        return _remove_origin(message)
    raise ValueError(f"Unsupported message type: {message_type or '(missing)'}")


def main() -> int:
    try:
        message = _read_message()
        response = _handle_message(message)
    except Exception as exc:
        response = {"ok": False, "error": str(exc)}
    _write_message(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
