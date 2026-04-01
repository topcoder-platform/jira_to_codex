#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENV_FILE="${JIRA_TO_CODEX_ENV_FILE:-$SCRIPT_DIR/.env}"

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing environment file: $ENV_FILE" >&2
  echo "Create $SCRIPT_DIR/.env from $SCRIPT_DIR/.env.example before running this script." >&2
  exit 1
fi

set -a
. "$ENV_FILE"
set +a

if [ -z "${PROJECT_ROOT:-}" ] && [ -n "${REPO_DIR:-}" ]; then
  export PROJECT_ROOT="$REPO_DIR"
fi

export PROMPT_TEMPLATE_PATH="${PROMPT_TEMPLATE_PATH:-$SCRIPT_DIR/codex/jira_fix_prompt.txt}"
export IGNORE_LIST_PATH="${IGNORE_LIST_PATH:-$SCRIPT_DIR/ignore_list.txt}"

python3 "$SCRIPT_DIR/run_jira_codex.py"
