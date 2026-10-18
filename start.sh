#!/bin/bash
set -e
timeout 900 xvfb-run -a python daily_triage.py
