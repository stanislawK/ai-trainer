# Pinned tool versions shared by build_css.sh (one-shot) and watch_css.sh (dev --watch,
# #63) — sourced only, so the two never drift apart on which tailwindcss/daisyUI build
# a developer ends up running (ADR-0012).
TAILWIND_VERSION="${TAILWIND_VERSION:-4.3.3}"
DAISYUI_VERSION="${DAISYUI_VERSION:-5.7.43}"
