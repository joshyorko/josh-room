#!/usr/bin/env bash
set -euo pipefail

brew install age uv
uv sync --group dev
