#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$project_dir"
if [ ! -x .venv/bin/python ]; then
    echo "Run scripts/setup.sh first." >&2
    exit 1
fi
exec .venv/bin/python -m tokensql.workspace.service "$@"
