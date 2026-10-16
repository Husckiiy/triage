#!/bin/bash
set -e

# roda com display virtual (obrigatorio pro headless=False)
exec xvfb-run -a --server-args="-screen 0 1920x1080x24" python daily_triage.py