#!/usr/bin/env bash
# Convenience launcher — keeps the venv out of your shell.
cd "$(dirname "$0")"
exec .venv/bin/python -m ollie "$@"
