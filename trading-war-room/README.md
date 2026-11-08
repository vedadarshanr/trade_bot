# Trading War Room

Autonomous **paper-mode** scalp & smart-money copy-trading engine.

- **Python** owns all ingestion, smart-wallet tracking, X-velocity proxies, and HFT math.
- **6 Grok seats** only read pre-computed packets and vote via `cli_vote.py`.

## Quick start (paper / zero keys)

```bash
cd trading-war-room
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Unit tests
pytest -q

# Long-lived daemon (leaderboard + scanners + exit cycle)
tmux new-session -d -s war_room_daemon -- .venv/bin/python -m war_room_engine.daemon
tmux attach -t war_room_daemon   # optional
```

Bot seats:

```bash
python3 war_room_engine/cli_vote.py list-pending
python3 war_room_engine/cli_vote.py vote --packet <ID> --seat OnChain-Auditor --json '{"audit_and_copy_score":80,"approve":true}'
python3 war_room_engine/cli_vote.py execute-paper --packet <ID>
python3 war_room_engine/cli_vote.py run-exit-cycle
```

## Layout

| Path | Purpose |
|---|---|
| `AGENTS.md` | 6-seat architecture + Stage-6 formula |
| `DECISIONS.md` | `MODE=PAPER` + hard risk gates |
| `SCALP_PLAYBOOK.md` | Polymarket + Pump.fun exit ladder |
| `SMART_WALLETS.md` | Auto-refreshed leaderboard tables |
| `war_room_engine/` | Deterministic engine package |
| `state/war_room.db` | SQLite telemetry |
| `state/PIPELINE-EVIDENCE.jsonl` | Append-only candidate packets |

## Safety

Defaults: `DRY_RUN=true`, `LIVE_TRADING_ENABLED=false`, no private keys required.
Public endpoints only (Gamma, CLOB book, Polymarket data-api, pumpportal websocket).
