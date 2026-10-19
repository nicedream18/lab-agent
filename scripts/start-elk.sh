#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
COMPOSE_PROFILES="${COMPOSE_PROFILES:+$COMPOSE_PROFILES,}elk"
export COMPOSE_PROFILES
exec "$project_dir/scripts/start-stack.sh" "$@"
