#!/bin/sh
# API entrypoint: migrate, then serve.
#
# The migration is retried because `pg_isready` can report healthy while
# Postgres is still in recovery mode after an unclean shutdown. A single
# attempt fails there, and although this container would restart and succeed,
# the workers gating on `api: service_healthy` have already given up by then.
set -e

attempt=0
max_attempts=${MIGRATION_ATTEMPTS:-10}

until alembic upgrade head; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge "$max_attempts" ]; then
        echo "migrations failed after ${attempt} attempts - giving up" >&2
        exit 1
    fi
    echo "migration attempt ${attempt} failed (database not ready?) - retrying" >&2
    sleep 3
done

exec uvicorn api.main:app --host 0.0.0.0 --port 8000 "$@"
