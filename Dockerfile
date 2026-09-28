# ADR-0012: compile Tailwind CSS v4 + daisyUI 5 with the standalone CLI, no Node
# anywhere. This stage is discarded — curl, the CLI binary and the daisyUI plugin
# bundle never reach the final image, only the compiled stylesheet does.
FROM python:3.14-slim-trixie AS css-builder
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY scripts/build_css.sh scripts/css_versions.sh scripts/css_tools.sh scripts/
COPY src/ai_trainer/web/static/css/input.css src/ai_trainer/web/static/css/input.css
COPY src/ai_trainer/web/static/css/theme.css src/ai_trainer/web/static/css/theme.css
COPY src/ai_trainer/web/static/css/glass.css src/ai_trainer/web/static/css/glass.css
COPY src/ai_trainer/web/templates src/ai_trainer/web/templates
RUN ./scripts/build_css.sh

# ADR-0002: pin the uv binary, sync deps without the project, copy source, sync again.
FROM python:3.14-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/

# curl is only ever invoked by scripts/watch_css.sh (#63), and only when compose.dev.yaml's
# dev_start.sh overrides the CMD below — `make up`/production never run it, but the tool
# still has to be in this image since watch_css.sh runs inside the running container, not
# at build time.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 999 nonroot \
 && useradd --system --gid 999 --uid 999 --create-home nonroot

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

COPY . /app
COPY --from=css-builder /app/src/ai_trainer/web/static/css/app.css \
    src/ai_trainer/web/static/css/app.css
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

# nonroot only ever needs to write here: scripts/watch_css.sh (#63) recompiles app.css and
# fetches the daisyUI bundles into this directory from inside the running container. -R:
# the files already inside it (COPY'd in as root above) need to change owner too, not just
# the directory — an open()-and-truncate on a root-owned file would still fail otherwise.
RUN chown -R nonroot:nonroot src/ai_trainer/web/static/css

ENV PATH="/app/.venv/bin:$PATH"

USER nonroot

EXPOSE 8000

# The probe's own timeout must exceed the DB ping's connect_timeout inside
# /health (2s default), or a slow-to-fail DB can time out the probe itself
# before the route gets to report "unavailable" gracefully.
HEALTHCHECK --interval=5s --timeout=6s --start-period=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)"

CMD ["uvicorn", "ai_trainer.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000"]
