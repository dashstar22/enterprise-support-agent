#!/bin/sh
# Apply the audited PostgreSQL schema before serving traffic. / 先应用审计数据库结构，再开始接收请求。
set -eu

for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if uv run --no-sync alembic upgrade head; then
    break
  fi
  if [ "$attempt" -eq 10 ]; then
    echo "Database migration failed after 10 attempts." >&2
    exit 1
  fi
  sleep 1
done
exec uv run --no-sync uvicorn app.main:app --host 0.0.0.0 --port 8000
