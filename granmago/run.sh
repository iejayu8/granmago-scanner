#!/usr/bin/with-contenv bashio

export DATA_DIR=/data
export PORT=8787

# 1.5.0 saved every strategy knob. Those keys are gone from the schema.
# Supervisor only warns about unknown options, but a later save can reject
# them. Drop them when the Supervisor API answers. If it does not, the
# process still starts: server.py ignores these keys.
options_json="$(bashio::addon.options 2>/dev/null || true)"
obsolete=(
  scan_seconds
  trailing_enabled
  trailing_activation_pct
  trailing_distance
  entry_threshold
  partial_tp_r
  partial_tp_fraction
  partial_move_breakeven
  stop_mode
  stop_atr_period
  stop_atr_mult
  trailing_activation_r
  trailing_keep_tp
  pair_cooldown_minutes
  max_same_direction_risk_r
  regime_filter
  regime_adx_min
  regime_er_min
  strategy
  granmago_variant
  granmago_max_adds
  granmago_momentum_exit
  granmago_max_hold_bars
  granmago_breakeven_r
  granmago_min_rr
  granmago_stop_pad_pct
  granmago_style
  granmago_setups
)
for key in "${obsolete[@]}"; do
  if [ -n "${options_json}" ] && bashio::jq.exists "${options_json}" ".${key}"; then
    bashio::log.info "Quitando la opción obsoleta ${key}"
    bashio::addon.option "${key}" || bashio::log.warning "No se pudo quitar ${key}; el servidor la ignora"
  fi
done

export LOG_LEVEL="$(bashio::config 'log_level')"
export RISK_PCT="$(bashio::config 'risk_pct')"
export LEVERAGE="$(bashio::config 'leverage')"
export INITIAL_BALANCE="$(bashio::config 'initial_balance')"
export MAX_POSITIONS="$(bashio::config 'max_positions')"
export FEE_PCT_PER_SIDE="$(bashio::config 'fee_pct_per_side')"
export SLIPPAGE_PCT_PER_SIDE="$(bashio::config 'slippage_pct_per_side')"
# JSON array. server.py also accepts a comma-separated list. A missing key
# leaves the 28-pair monitoring book in place.
if bashio::config.exists 'pairs'; then
  export PAIRS="$(bashio::config 'pairs')"
fi

exec python -u /app/server.py
