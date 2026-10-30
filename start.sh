#!/bin/bash
set -e
timeout 600 xvfb-run -a --server-args="-screen 0 1920x1080x24" python daily_triage.py
exit 0
