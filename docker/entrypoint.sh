#!/bin/sh
# Xvfb wrapper for headed-browser paths (ghost/g2-selfcheck/CF fallback).
# Only the browser target sets SIGNALS_XVFB=1; base stays lean.
# Passthrough rule: bare CLI invocations are routed to `signals`; anything
# else (python -m pytest, bash, ...) is exec'd verbatim so the image stays a
# general-purpose runner for verification steps like Task 8's.
set -e
case "$1" in
  ""|doctor|init|status|accounts|seed|resolve|collect|score|brief|export|intel|run|watch|index|prune|digest)
    if [ "$SIGNALS_XVFB" = "1" ] && [ -z "$DISPLAY" ] && command -v xvfb-run >/dev/null 2>&1; then
      exec xvfb-run -a signals "$@"
    fi
    exec signals "$@" ;;
  *) exec "$@" ;;
esac
