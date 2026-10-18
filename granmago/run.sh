#!/usr/bin/with-contenv bashio

export DATA_DIR=/data
export PORT=8787
export LOG_LEVEL="$(bashio::config 'log_level')"
export RISK_PCT="$(bashio::config 'risk_pct')"
export LEVERAGE="$(bashio::config 'leverage')"
export INITIAL_BALANCE="$(bashio::config 'initial_balance')"
export SCAN_SECONDS="$(bashio::config 'scan_seconds')"
export TRAILING_ENABLED="$(bashio::config 'trailing_enabled')"
export TRAILING_ACTIVATION_PCT="$(bashio::config 'trailing_activation_pct')"
export TRAILING_DISTANCE="$(bashio::config 'trailing_distance')"
export ENTRY_THRESHOLD="$(bashio::config 'entry_threshold')"

exec python -u /app/server.py
