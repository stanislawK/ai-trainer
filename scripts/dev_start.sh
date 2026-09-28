#!/usr/bin/env bash
# `make start`'s dev container entrypoint (#63): runs the Tailwind watcher alongside
# `uvicorn --reload` in the same container, so the watcher writes app.css where the app
# serves it from. Only used by compose.dev.yaml's app service — the production image's CMD
# is untouched. Both processes share the container's lifecycle: `docker compose stop`/`down`
# tears the whole container down, so neither needs its own signal handling here.
set -euo pipefail

./scripts/watch_css.sh &

exec uvicorn ai_trainer.main:app_factory --factory --host 0.0.0.0 --port 8000 \
  --reload --reload-dir src
