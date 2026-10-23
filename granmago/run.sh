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
export MAX_POSITIONS="$(bashio::config 'max_positions')"
export FEE_PCT_PER_SIDE="$(bashio::config 'fee_pct_per_side')"
export SLIPPAGE_PCT_PER_SIDE="$(bashio::config 'slippage_pct_per_side')"
export TAKE_PROFIT_R="$(bashio::config 'take_profit_r')"
export PARTIAL_TP_R="$(bashio::config 'partial_tp_r')"
export PARTIAL_TP_FRACTION="$(bashio::config 'partial_tp_fraction')"
export PARTIAL_MOVE_BREAKEVEN="$(bashio::config 'partial_move_breakeven')"
export STOP_MODE="$(bashio::config 'stop_mode')"
export STOP_ATR_PERIOD="$(bashio::config 'stop_atr_period')"
export STOP_ATR_MULT="$(bashio::config 'stop_atr_mult')"
export TRAILING_ACTIVATION_R="$(bashio::config 'trailing_activation_r')"
export TRAILING_KEEP_TP="$(bashio::config 'trailing_keep_tp')"
export PAIR_COOLDOWN_MINUTES="$(bashio::config 'pair_cooldown_minutes')"
export MAX_SAME_DIRECTION_RISK_R="$(bashio::config 'max_same_direction_risk_r')"
export REGIME_FILTER="$(bashio::config 'regime_filter')"
export REGIME_ADX_MIN="$(bashio::config 'regime_adx_min')"
export REGIME_ER_MIN="$(bashio::config 'regime_er_min')"

exec python -u /app/server.py
