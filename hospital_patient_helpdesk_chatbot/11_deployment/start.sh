#!/usr/bin/env sh
set -eu
uvicorn 07_backend.12_api_main:app --host 0.0.0.0 --port 8000
