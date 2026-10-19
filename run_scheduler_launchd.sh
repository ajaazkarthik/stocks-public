#!/usr/bin/env bash
# Wrapper used by launchd to start the AjaazAlert Report Scheduler.
# launchd does not source shell profiles, so we source the env files explicitly.
#
# Credentials are NOT stored here. `scheduler_env.sh` is a tracked template that
# sources the ignored `scheduler_env.local.sh` when it exists.
set -a
cd /Users/priya/stocks || exit 1
source ./.venv/bin/activate
source ./scheduler_env.sh
exec python report_scheduler.py
