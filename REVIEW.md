# Paper-mode review

Shallow clones of `main`, read on 2026-11-08. No real keys were used.

| Repo | Commit | Paper/dry-run verdict |
|---|---|---|
| [grokbot-pumpfun](https://github.com/zostaff/grokbot-pumpfun) | `b430c35` (2026-08-27) | **Safe to run in dry-run.** No wallet. There is no Stage 1–2-only switch. |
| [poly-trading-bot](https://github.com/zostaff/poly-trading-bot) | `175274d` (2026-05-03) | **Safe for the unauthenticated market-ingest loop.** Full `cli.py run` is a different, key-using process. |
| [poly-arbitrage-bot](https://github.com/zostaff/poly-arbitrage-bot) | `8a1a123` (2026-05-06) | **Source is not malicious, but paper mode cannot start with zero keys.** It authenticates to both exchanges before scanning. |

None of the three trees contain `exec`/`eval` of remote payloads, base64 blobs, hidden downloaders, auto-updaters, or typosquatted package names. `base64` appears only as Kalshi RSA-PSS request signing in poly-arbitrage-bot.

---

## grokbot-pumpfun

### Security

**Safe to run in `mode: dry-run`.**

- Secrets stay local. `grok.api_key`, `data.api_key`, `solana.wallet_private_key`, and `alerts.webhook_url` are `SecretStr`. Startup logs a masked key (`pape…real`), not the value. The wallet is not read by `DryRunExecutor`.
- `LiveExecutor.buy` / `sell` raise `NotImplementedError`. Live also requires `mode: live` and `--i-understand-the-risk`. No signing code is generated.
- Network targets in the dry-run loop are the configured ones only: `wss://pumpportal.fun/api/data` (monitor) and `https://frontend-api.pump.fun` (analyzer, and price polls if a dry-run position exists). Grok is `POST https://api.x.ai/v1/chat/completions` with `Authorization: Bearer <grok key>`, and only after a token passes the analyzer. No Helius. Public Solana RPC and the Jito URL are config/doctor/live notes; the dry-run loop does not call them.
- Webhook posts are off when `alerts.webhook_url` is empty (the default). If set, the payload is event text, not keys.
- Dependencies in `requirements.txt` / `pyproject.toml` are the normal ones (`httpx`, `websockets`, `pydantic`, `pyyaml`, `solders`, `solana`, pytest). No `setup.py`, no install hooks, no auto-update.
- Footgun: `config.example.yaml` sets `data.api_key: "YOUR-DATA-PROVIDER-KEY"`. That string is non-empty, so the analyzer would send it as a Bearer token. Clear it to `""`. An empty env var does not override a non-empty yaml value.

### Install and Stage 1–2 dry-run

There is no screener subcommand. Stage 1 is `LaunchMonitor` (websocket filter) and stage 2 is `Analyzer` (three REST calls, then code). Both run inside `grokbot run` / `python -m src.cli run`. A token that passes stage 2 continues to the Grok agents, so a non-placeholder Grok key is required even if you only want the screener. In a 40s run nothing reached stage 2 (`min_age_seconds` is 120, plus buyer/metadata/curve filters).

```bash
git clone https://github.com/zostaff/grokbot-pumpfun.git
cd grokbot-pumpfun
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Edit `config.yaml`: `mode: dry-run`, `data.api_key: ""`, `solana.wallet_private_key: ""`, `alerts.webhook_url: ""`. Set `grok.api_key` to a real xAI key only if you accept that passing tokens will be sent to `api.x.ai`. Do not export `GROKBOT_WALLET_PRIVATE_KEY`.

```bash
tmux new-session -d -s grokbot -- .venv/bin/python -m src.cli run --config config.yaml
```

| Variable | Paper |
|---|---|
| `GROKBOT_GROK_API_KEY` | **Required** to pass `check_ready`, even for the screener. Sent to xAI only after stage 2 passes. |
| `GROKBOT_DATA_API_KEY` | Optional. Public pump.fun works without it (rate limits). Leave unset and blank the yaml placeholder. |
| `GROKBOT_WALLET_PRIVATE_KEY` | Not used in dry-run. Required only for `mode: live`, which still cannot sign. |
| `GROKBOT_RPC_URL` | Not used by the dry-run loop. |
| `GROKBOT_ALERT_WEBHOOK` | Optional. Leave unset. |
| `GROKBOT_LOG_PATH`, `GROKBOT_STATE_PATH`, `GROKBOT_MODE`, `GROKBOT_LOG_LEVEL`, `GROKBOT_HEALTH_PORT` | Optional. |

Verified: full pytest suite passed. `check` with an empty Grok key exited 1. With a dummy key, a 40s `run` connected to `wss://pumpportal.fun/api/data`, wrote two `skip` lines at `stage: monitor`, made 0 Grok calls, and shut down cleanly on SIGTERM. No wallet was set.

### Evidence hook

Yes. Append-only JSONL at `logging.path`, default **`logs/trades.jsonl`** relative to the process cwd (`GROKBOT_LOG_PATH` overrides). Writer: `TradeLog._write` in `src/log.py`. One JSON object per line, flushed immediately. Rotates at 50MB to `trades.jsonl.1` … `.5`.

Record `type`: `skip` (fields `stage`, `reason`, `mint`, `symbol`, `detail`, `scores`), `intent`, `buy`, `close` (close carries `pnl_sol` / `pnl_pct`). Stage 1 lines are `stage: "monitor"`. Stage 2 rejects are `stage: "analyzer"`. That file is the adapter input; nothing else emits JSONL.

### Database

**No SQLite.** Runtime state is JSON:

- `ops.state_path`, default `state/pipeline.json` (`src/state.py`, `PipelineState`: `day`, `trades_today`, `realized_pnl_sol`, `grok_calls_today`, `losing_streak`, `cooldown_until`, `positions`)
- `ops.reputation_path`, default `state/creators.json` (`src/reputation.py`)

Positions and PnL for a dashboard are the JSONL `buy`/`close` lines plus `pipeline.json`.

---

## poly-trading-bot

### Security

**Safe for paper ingestion with no wallet.** The trading CLI is a separate path.

- No code sends env vars or keys to an unexpected host. Gamma discovery is unauthenticated. CLOB signing uses the official `py-clob-client` (PyPI `py_clob_client`, homepage `github.com/Polymarket/py-clob-client`) and only when an authenticated call runs.
- Expected hosts: `https://gamma-api.polymarket.com`, `https://clob.polymarket.com`, `https://data-api.polymarket.com`, `https://polygon-rpc.com`, `https://openrouter.ai/api/v1`, plus optional news RSS in `src/config/settings.py` (not used by ingest). No Helius.
- `setup.py` is an interactive checker (`if __name__ == "__main__"`). It is not a setuptools `setup()` and nothing runs it on install. `scripts/install_beast_mode.py` shells out to local `pip` / `python -c` only if you run that script. No postinstall hook, no auto-updater.
- `sentry-sdk==1.39.2` and `aiohttp==3.9.1` are pinned and **never imported**. No DSN. `pickle` is a local cost file, `logs/daily_openrouter_usage.pkl`.
- Pins are old (`aiohttp` 3.9.1, `requests` 2.31.0, `cryptography` 42.0.0). That is maintenance risk, not a hidden payload.
- `Settings.validate()` prints an error when `POLYMARKET_PRIVATE_KEY` is missing and then continues. Ingest still runs.

### Install and 60-second ingest

`TradingConfig.scan_interval_seconds = 60` is unused. Inside `BeastModeBot`, ingest sleeps **300s** (`_run_market_ingestion`); the **60s** sleep is the full trading cycle, which builds `PolymarketClient` and `XAIClient`. `--interval` applies only to `cli.py run --safe-compounder --loop` (default 300) and that path calls `get_orderbook`, which requires a private key even in dry-run. `scripts/dry_run_smoke.py` is a one-shot public read and writes no database.

Ingest-only loop, no keys (this is the ~60s job). `run_ingestion` itself is one pass; the sleep is the loop:

```bash
git clone https://github.com/zostaff/poly-trading-bot.git
cd poly-trading-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
unset POLYMARKET_PRIVATE_KEY OPENROUTER_API_KEY POLYMARKET_FUNDER
export DRY_RUN=true LIVE_TRADING_ENABLED=false
tmux new-session -d -s poly-ingest -- .venv/bin/python -c '
import asyncio
from src.jobs.ingest import run_ingestion
from src.utils.database import DatabaseManager

async def main():
    db = DatabaseManager("trading_system.db")
    await db.initialize()
    while True:
        await run_ingestion(db, asyncio.Queue(), polymarket_client=None)
        await asyncio.sleep(60)

asyncio.run(main())
'
```

| Variable | This ingest loop | Full `python cli.py run --paper` |
|---|---|---|
| `POLYMARKET_PRIVATE_KEY` | Not required | Required for CLOB, balances, orderbooks. Orders are still skipped while `live_mode` is false. |
| `OPENROUTER_API_KEY` | Not required | Required for the AI cycle. |
| `POLYGON_RPC_URL` | Not used | Optional. Default `https://polygon-rpc.com`. |
| `POLYMARKET_FUNDER`, `POLYMARKET_SIGNATURE_TYPE` | Not used | Optional. EOA default is signature type 0. |
| `POLYMARKET_GAMMA_HOST` | Optional. Default Gamma host. | Same. |
| `POLYMARKET_HOST`, `POLYMARKET_CHAIN_ID` | Not used by Gamma ingest | Optional CLOB overrides. |
| `DAILY_AI_COST_LIMIT` | Not used | Optional. Default 10. |
| `DATABASE_URL` | **Ignored.** | **Ignored.** |

Do not run `cli.py health --full` without a key; `--full` derives CLOB API credentials. Plain `health` tries a balance read and will fail that check.

Verified: one `run_ingestion` with no key, ~5s, Gamma `https://gamma-api.polymarket.com`, 1998 rows in `markets`. The missing-key warning printed and did not abort.

### Evidence hook

No JSONL candidate file. Ingest writes SQLite via `DatabaseManager.upsert_markets` (`src/utils/database.py`). The in-process queue is discarded by Beast Mode. Structlog lines go to the console (and, if `setup_logging` was used, `logs/trading_system_<timestamp>.log`); they are not a JSONL evidence stream.

Adapter point: read new `markets` rows after each ingest, or wrap `run_ingestion`'s queue. `src/paper/tracker.py` is a second database (`signals`), used by `paper_trader.py`, not by this loop.

### Database

Default path is **`trading_system.db` in the process cwd**. `DatabaseManager("trading_system.db")`. `DATABASE_URL` in `env.template` is not read. `DB_PATH` is read only by `python -m src.utils.database`.

Module: `src/utils/database.py`. Tables created by `initialize()`:

- `markets` — `market_id` PK, `title`, `yes_price`, `no_price`, `volume`, `expiration_ts`, `category`, `status`, `last_updated`, `has_position`
- `positions` — `id`, `market_id`, `side`, `entry_price`, `quantity`, `timestamp`, `rationale`, `confidence`, `live`, `status`, `strategy`, `stop_loss_price`, `take_profit_price`, `max_hold_hours`, `target_confidence_change`. Unique `(market_id, side)`
- `trade_logs` — closed trades: `entry_price`, `exit_price`, `quantity`, `pnl`, timestamps, `rationale`, `strategy`
- `market_analyses` — decisions: `market_id`, `analysis_timestamp`, `decision_action`, `confidence`, `cost_usd`, `analysis_type`
- `daily_cost_tracking` — `date` PK, `total_ai_cost`, `analysis_count`, `decision_count`
- `llm_queries` — prompt/response, tokens, cost, extracted confidence/decision
- `analysis_reports` — health-score reports

Created later, same file, only if those strategies start:

- `category_scores`, `category_trade_log` (`src/strategies/category_scorer.py`)
- `blocked_trades` (`src/strategies/portfolio_enforcer.py`)

Separate paper DB: `PAPER_TRADING_DB` or `data/paper_trades.db`, table `signals` (`src/paper/tracker.py`).

---

## poly-arbitrage-bot

### Security

**No malware in the tree. Not a no-key paper bot.**

- Paper fills are simulated in `Executor._execute_paper` (`venue_order_id` like `PAPER-…`, no `place_order`). Live orders require `LIVE_TRADING_ENABLED=true` and `python scripts/live_run.py --live`. Verified: `live_run.py` without `--live` exits 1 before any auth check.
- `scripts/paper_run.py` still calls `build_clients()`, which requires `POLYMARKET_PK` and a Kalshi PEM, constructs `ClobClient(key=…)`, and loads the RSA key. The scan then signs every Kalshi request and can call `create_or_derive_api_creds` on the Polymarket balance snapshot. Those calls go to the exchanges, not to a third host.
- Hosts: Gamma `https://gamma-api.polymarket.com`, CLOB `https://clob.polymarket.com`, Kalshi demo `https://demo-api.kalshi.co/trade-api/v2` (default `KALSHI_ENV=demo`) or prod `https://api.elections.kalshi.com/trade-api/v2`.
- `base64` is only the Kalshi signature header. No `eval`/`exec`, no install hook (`pyproject.toml` + setuptools), no auto-updater. Installed `py-clob-client` 0.34.6 is Polymarket's package.

### Install and paper run

```bash
git clone https://github.com/zostaff/poly-arbitrage-bot.git
cd poly-arbitrage-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env
# LIVE_TRADING_ENABLED=false
# POLYMARKET_PK=<polygon key>          # required, even for paper
# KALSHI_KEY_ID=<id>                   # required
# KALSHI_PRIVATE_KEY_PATH=</abs/key.pem>  # required
# KALSHI_ENV=demo
tmux new-session -d -s poly-arb -- .venv/bin/python scripts/paper_run.py
```

| Variable | Paper |
|---|---|
| `POLYMARKET_PK` | **Required.** Empty value aborts before the scan. |
| `KALSHI_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` | **Required.** File must exist. |
| `KALSHI_ENV` | Optional. Default `demo`. |
| `POLYMARKET_FUNDER`, `POLYMARKET_SIGNATURE_TYPE` | Optional. EOA is type 0 and an empty funder. |
| `POLYMARKET_HOST`, `POLYMARKET_CHAIN_ID` | Optional. Defaults CLOB mainnet / chain 137. |
| `LIVE_TRADING_ENABLED` | Leave `false`. |
| `DB_PATH` | Optional. Default `data/poly_arb.db`. |
| `SCAN_INTERVAL_SECONDS` | Optional. Default **5**, not 60. |
| `MIN_EDGE_BPS`, `MAX_POSITION_USD`, `QUARTER_KELLY`, `DRAWDOWN_LIMIT`, `MATCH_*`, `OPPORTUNITY_TTL_MS`, `LOG_LEVEL` | Optional tuning. |

Verified: `pytest` 37 passed. `paper_run.py` with an empty environment logged one JSON line (`mode: paper`) and raised `RuntimeError: POLYMARKET_PK is empty`. It had already created the empty schema at `data/poly_arb.db`. The scan was not run.

### Evidence hook

No candidate JSONL file. Two hooks:

- **stdout JSON lines** from `src/utils/logger.py` (`JsonFormatter` on the root logger). A paper opportunity log is `msg: "opportunity"` plus `type`, `edge_bps`, `net`, `decision`, `review_required`. Redirect stdout if the adapter wants a file. This is not the full candidate.
- **SQLite `opportunities`** is the full record (`src/storage/db.py` `insert_opportunity`). That is the better adapter input.

### Database

Path: `Settings.db_full_path`. Default **`<repo>/data/poly_arb.db`** (`DB_PATH` is resolved from the repo root, not the cwd). Module `src/storage/db.py`, schema `src/storage/schema.sql`. WAL, foreign keys on.

- `opportunities` — detection and the decision: `detected_at`, `arb_type` (`cross_exchange`|`bundle`), poly/kalshi ids and questions, `similarity`, both legs (`side`, `price`, `venue`), `gross_edge_usd`, `fees_usd`, `slippage_usd`, `net_edge_usd`, `edge_bps`, `sized_usd`, `decision`, `decision_reason`
- `trades` — `opportunity_id`, `started_at`, `finished_at`, `mode` (`paper`|`live`), `status`, `realized_pnl`, `notes`
- `legs` — per-leg fill: `trade_id`, `venue`, `market_id`, `side`, sizes and prices, `fee_usd`, `venue_order_id`, `status`, timestamps
- `balance_snapshots` — `captured_at`, `venue`, `balance_usd`, `note`

There is no separate PnL or decisions table. PnL is `trades.realized_pnl`. The decision is `opportunities.decision`.
