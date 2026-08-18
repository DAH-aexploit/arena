#!/bin/sh
set -e
mkdir -p /flags
first=1
IFS=','
for name in ${FLAG_ENVS:-}; do
  val=$(printenv "$name" 2>/dev/null || true)
  printf %s "$val" > "/flags/$name"
  if [ "$first" = 1 ]; then printf %s "$val" > "${FLAG_PATH:-/flag}"; first=0; fi
  unset "$name"
done
unset FLAG_ENVS
exec "$@"
