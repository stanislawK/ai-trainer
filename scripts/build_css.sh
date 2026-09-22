#!/usr/bin/env bash
# Compiles Tailwind CSS v4 + daisyUI 5 with the standalone CLI — no Node, ever (ADR-0012).
# Used by the Dockerfile's css-builder stage and by `make css` for local dev.
set -euo pipefail

TAILWIND_VERSION="${TAILWIND_VERSION:-4.3.3}"
DAISYUI_VERSION="${DAISYUI_VERSION:-5.7.43}"
WEB_DIR="src/ai_trainer/web"
CSS_DIR="$WEB_DIR/static/css"
TOOLS_DIR="$(mktemp -d)"
trap 'rm -rf "$TOOLS_DIR"' EXIT

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"
case "$os-$arch" in
  linux-x86_64) tw_platform=linux-x64 ;;
  linux-aarch64) tw_platform=linux-arm64 ;;
  darwin-arm64) tw_platform=macos-arm64 ;;
  darwin-x86_64) tw_platform=macos-x64 ;;
  *)
    echo "build_css.sh: unsupported platform $os-$arch" >&2
    exit 1
    ;;
esac

curl -sLfo "$TOOLS_DIR/tailwindcss" \
  "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-${tw_platform}"
chmod +x "$TOOLS_DIR/tailwindcss"

# daisyui.js is a build-time Tailwind plugin, not a runtime asset — it must sit
# next to input.css for the `@plugin "./daisyui.js"` directive to find it, so it
# lands in the tracked (though gitignored) source tree rather than $TOOLS_DIR.
# The cleanup trap is registered *before* the download — not after — so a failed
# or partial fetch (network drop, bad version pin) can never leave it behind for
# a later `docker build`'s `COPY . /app` to sweep into the final image.
trap 'rm -rf "$TOOLS_DIR" "$CSS_DIR/daisyui.js"' EXIT
curl -sLfo "$CSS_DIR/daisyui.js" \
  "https://github.com/saadeghi/daisyui/releases/download/v${DAISYUI_VERSION}/daisyui.js"

"$TOOLS_DIR/tailwindcss" -i "$CSS_DIR/input.css" -o "$CSS_DIR/app.css" --minify
