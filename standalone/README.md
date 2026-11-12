# GranMago standalone (box, PAPER only)

The same GranMago 1.6.2 engine (`granmago/server.py`, unchanged) running outside
Home Assistant, plus an append-only fiscal journal and a readable status.
It has no Home Assistant or Supervisor dependency, uses no exchange keys and sends
no orders. It reads public Blofin market data only. `HA_TOKEN`, `SUPERVISOR_TOKEN`
and `HASSIO_TOKEN` are removed from the engine's environment.

## Run

```bash
standalone/granmagoctl.sh start     # watchdog + engine in background (setsid/nohup); idempotent
standalone/granmagoctl.sh ensure    # same as start, for periodic routines (no-op when up)
standalone/granmagoctl.sh status    # pids + GET /status summary
standalone/granmagoctl.sh logs 100  # tail engine.log
standalone/granmagoctl.sh stop      # TERM to watchdog -> TERM to engine (KILL after 10 s)
```

Foreground, without the watchdog: `python3 standalone/granmago_standalone.py`.

| Item | Default | Env override |
|---|---|---|
| Data dir (state.json, journal, status.json) | `/workspace/granmago-box/data` | `DATA_DIR` |
| Bind | `127.0.0.1:8797` | `GRANMAGO_HOST`, `PORT` |
| Logs | `/workspace/granmago-box/logs/{engine,watchdog}.log` | `GRANMAGO_LOG_DIR` |
| Pid files | `/workspace/granmago-box/run/{watchdog,engine}.pid` | `GRANMAGO_RUN_DIR` |
| Risk / leverage / slots | 1 % / 3x / 3 | `RISK_PCT`, `LEVERAGE`, `MAX_POSITIONS` |
| Fee / slippage per side | 0.06 % / 0.02 % | `FEE_PCT_PER_SIDE`, `SLIPPAGE_PCT_PER_SIDE` |
| Pairs | the 28 `LIQUID_PAIRS` | `PAIRS` (empty = 28) |
| Initial paper balance (new data dir only) | 1000 USDT | `INITIAL_BALANCE` |
| Autostart scanning | on | `GRANMAGO_AUTOSTART=0` |
| status.json period / FX retry period | 15 s / 1800 s | `GRANMAGO_STATUS_INTERVAL`, `GRANMAGO_FX_INTERVAL` |

TP 1.7R and the other fixed behaviour are pinned by the engine (`FIXED_BEHAVIOUR`).

Watchdog: a bash restart loop. Backoff is 5 s doubling to 300 s and resets after
a run longer than 600 s. The engine's stdout and stderr go straight to
`engine.log`, a file and never a pipe, so output can never block it.

Heartbeat: every 30 s the watchdog runs `heartbeat_check.py`. If `status.json`
has not been refreshed for 300 s, or the engine has had no cycle for 600 s, the
watchdog:
1. sends `SIGUSR1` to the engine (faulthandler writes every thread's stack to `engine.log`);
2. restarts it;
3. writes a `HEARTBEAT` line to `watchdog.log`.

Both ages are measured in run time (CLOCK_MONOTONIC, the clock behind `/proc/uptime`).

## Box pauses (important)

The Grok Bot box is a VM that the platform pauses a few minutes after no bot is
working on it. Background processes do not count as activity. During a pause
everything freezes: the engine, the watchdog and CLOCK_MONOTONIC. On resume the
wall clock jumps forward. The engine can neither prevent nor run through a pause.

What it does is detect each one:
- `status.json` gains `suspensions` {count, totalSeconds, last, recent};
- the log gets a line "Caja en pausa N min ... reanudado";
- the engine starts a fresh cycle (`kick()`).

Health and the heartbeat use run-time ages, so a pause is never reported as a
hang and never causes a restart. `lastCycleAgeSeconds` is the wall-time age;
`lastCycleRunAgeSeconds` is the run-time age.

Start is idempotent and checks the pid against the real command line, so
`granmagoctl.sh ensure` is safe to run from a periodic routine.

The box has no systemd as init and no cron, so nothing restarts the engine
after a box update or restart. Either a routine runs `ensure`, or it is
started by hand.

## Status

`GET http://127.0.0.1:8797/status` returns the same JSON as `DATA_DIR/status.json`.
The file is rewritten atomically (tmp + fsync + rename) every 15 s.

Main keys:
- identity and health: `health` (`ok`/`starting`/`degraded`/`stale`/`stopped`; `stale` = no cycle for 600 s of run time), `version`, `mode` (`paper`), `realMoney` (false), `running`, `pid`, `bind`;
- uptime: `startedAt`, `uptimeSeconds`;
- account: `balance`, `initialBalance`, `openPositions[]`, `openCount`;
- cycles: `lastCycleAt`, `lastCycleAgeSeconds`, `lastScanAt`;
- errors: `recentErrors[]` (WARNING+ log records), `journalErrors[]`;
- `config`, `journal` {rows, fx_pending, path, fx}, `recentLog`.

It also has `monotonicAt`, `lastCycleRunAgeSeconds`, `wallUptimeSeconds`, `suspensions` and `heartbeat`. The schema is now 2.

## Fiscal journal

The journal is `DATA_DIR/fiscal_journal.sqlite` (SQLite, WAL). A CSV export of
the `trades_eur` view is written to `DATA_DIR/fiscal_journal.csv`. The export is
regenerated atomically and never edited by hand.

Readers should open the database read-only:
`sqlite3 'file:/workspace/granmago-box/data/fiscal_journal.sqlite?mode=ro'`.

- **Append-only.** Triggers abort any `UPDATE` or `DELETE` on `trades` and
  `fx_resolutions`. `INSERT OR REPLACE` on an existing key is ignored, so the
  original row survives.
- **Idempotent.** `trade_key` = sha256(instance|mode|tradeId|open time|pair|side|leg)[:32]
  is UNIQUE, and inserts are `INSERT OR IGNORE`.
- **One row per closed leg.** A partial take-profit is leg 1 with `partial=1`.
  The final close is leg N with `partial=0`.
- **Paper is not fiscal.** Paper rows have `mode='paper'` and `fiscal=0`. Order
  IDs are empty, and funding is 0 with `funding_status='not_modelled_paper'`.
- **EUR.** The rate is the ECB reference rate, USD per EUR, from
  `eurofxref-hist-90d.xml`, with USDT treated as USD.
  - A row closed on Madrid date D uses the rate for D.
  - If D is past and has no fixing (weekend or holiday), it uses the last
    fixing before D.
  - If D is today and the ECB has not published yet, the row stays
    `fx_status='pending'`.
  - If the ECB cannot be reached, the trade is still recorded without EUR and
    with `fx_source='pending'`. It is retried every `GRANMAGO_FX_INTERVAL`.
  - The resolved rate is appended to `fx_resolutions`, and `trades` is never
    touched.
  - Network I/O never happens under the engine lock.

### Table `trades`

| column | type | meaning |
|---|---|---|
| seq | INTEGER PK | insertion order |
| trade_key | TEXT UNIQUE | idempotency key |
| recorded_at_utc | TEXT | when the row was written (UTC, `Z`) |
| mode | TEXT | `paper` / `demo` / `real` |
| fiscal | INTEGER | 1 only for `real` |
| engine_version, instance | TEXT | e.g. `1.6.2`, `box` |
| local_trade_id | INTEGER | engine `tradeId` |
| leg, partial | INTEGER | leg number; 1 = partial close |
| open_time_utc, open_time_madrid | TEXT | entry time (ISO, UTC `Z` / Europe/Madrid offset) |
| close_time_utc, close_time_madrid | TEXT | exit time |
| close_date_madrid | TEXT | `YYYY-MM-DD`, the FX date key |
| pair, side | TEXT | `XMR-USDT`, `long`/`short` |
| quantity_base | REAL | notional / entry price (base units) |
| notional_usdt | REAL | closed notional |
| entry_price, exit_price | REAL | fills (exit includes paper slippage) |
| fees_usdt | REAL | exit fee + this leg's share of the entry fee |
| funding_usdt, funding_status | REAL, TEXT | 0 / `not_modelled_paper` in paper |
| pnl_usdt | REAL | net PnL of this leg (after fees) |
| r_multiple, reason | REAL, TEXT | from the engine |
| entry_order_id, exit_order_id, client_order_id, tpsl_id, exchange_trade_ids | TEXT | empty in paper |
| signal_close_ms | INTEGER | candle close of the signal |
| raw_json | TEXT | engine history row + position (audit) |
| row_sha256 | TEXT | sha256 of the row's fields (except recorded_at_utc) |

### Table `fx_resolutions` (append-only)

The columns are: `seq`, `trade_key` (UNIQUE, FK), `usd_per_eur`, `rate_date`,
`source` and `fetched_at_utc`.

### View `trades_eur` (also the CSV)

It holds every `trades` column plus:
- `fx_status`: `pending` or `resolved`;
- `fx_usd_per_eur`, `fx_rate_date`;
- `fx_source`: `pending` when unresolved;
- `fx_fetched_at_utc`;
- `pnl_eur` = round(pnl_usdt / usd_per_eur, 2);
- `fees_eur`.

`meta` holds `schema_version` (1) and `created_at_utc`.

## Tests

`python3 -m unittest discover -s tests` runs the existing suite plus
`test_fiscal_journal.py`, `test_standalone.py` and `test_standalone_liveness.py`. The latter starts the real
runner on a free loopback port with autostart off, so it makes no network calls.
