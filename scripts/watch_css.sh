#!/usr/bin/env bash
# Recompiles app.css whenever a template, input.css, theme.css or glass.css changes
# (ADR-0012 dev CSS path, #63). Only ever runs inside `make start`'s dev container
# (compose.dev.yaml's dev_start.sh), never in the css-builder stage or `make css` — its
# downloaded tools and daisyUI bundles stay inside that ephemeral container, so they never
# reach the host or git. Reuses build_css.sh's pinned tool versions.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/css_versions.sh"
source "$SCRIPT_DIR/css_tools.sh"

WEB_DIR="src/ai_trainer/web"
CSS_DIR="$WEB_DIR/static/css"
TOOLS_DIR="$(mktemp -d)"

fetch_css_tools "$TOOLS_DIR" "$CSS_DIR"

# --watch=always: a container's stdin is closed, and plain --watch exits as soon as stdin
# closes — =always keeps the watcher running regardless. --minify matches the production
# build's flags so dev app.css stays close in size to what css-builder produces (the #8
# gotcha is about unwanted scanned content, not minification, and input.css's `source(none)`
# fix already applies here unchanged). No --poll: Compose Watch's `sync` action
# (compose.dev.yaml) tar-copies real writes into the container's own filesystem rather than
# a live bind mount, which inotify already picks up reliably.
exec "$TOOLS_DIR/tailwindcss" -i "$CSS_DIR/input.css" -o "$CSS_DIR/app.css" --watch=always --minify
