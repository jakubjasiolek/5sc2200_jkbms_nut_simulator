#!/bin/bash
# run-driver.sh <driver> <ups-name> [wait-file]  - foreground NUT driver for supervisord
set -e
for d in /usr/lib/nut /lib/nut /usr/libexec/nut; do [ -x "$d/$1" ] && DRV="$d/$1" && break; done
[ -n "${DRV:-}" ] || { echo "driver $1 not found"; exit 1; }
if [ -n "${3:-}" ]; then
  for _ in $(seq 1 120); do [ -s "$3" ] && break; sleep 1; done
fi
exec "$DRV" -a "$2" -F -u root
