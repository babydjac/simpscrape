#!/bin/sh
set -e

SOURCE_PATH="$0"
while [ -h "$SOURCE_PATH" ]; do
  SOURCE_DIR=$(CDPATH= cd -P -- "$(dirname -- "$SOURCE_PATH")" && pwd)
  SOURCE_TARGET=$(readlink "$SOURCE_PATH")
  case "$SOURCE_TARGET" in
    /*) SOURCE_PATH="$SOURCE_TARGET" ;;
    *) SOURCE_PATH="$SOURCE_DIR/$SOURCE_TARGET" ;;
  esac
done

SCRIPT_DIR=$(CDPATH= cd -P -- "$(dirname -- "$SOURCE_PATH")" && pwd)
REPO_ROOT=$(CDPATH= cd -P -- "$SCRIPT_DIR/.." && pwd)
VENV_PYTHON="$REPO_ROOT/.venv/bin/python"

if [ -x "$VENV_PYTHON" ]; then
  exec "$VENV_PYTHON" "$SCRIPT_DIR/native_host.py" "$@"
fi

exec python3 "$SCRIPT_DIR/native_host.py" "$@"
