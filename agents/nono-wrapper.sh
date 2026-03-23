#!/usr/bin/env bash
# Wraps claude CLI with nono kernel-level sandbox enforcement.
# Environment variables NONO_PROFILE and NONO_FLAGS are set by agent.py.
set -euo pipefail
exec nono run \
    --config "${NONO_PROFILE}" \
    ${NONO_FLAGS:-} \
    -- claude "$@"
