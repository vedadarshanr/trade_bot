# bot_trade

Contains:

1. **`REVIEW.md`** — paper-mode security review of three third-party bots.
2. **`trading-war-room/`** — autonomous scalp & smart-money copy-trading engine (paper mode).

## Trading War Room

```bash
cd trading-war-room
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q
tmux new-session -d -s war_room_daemon -- .venv/bin/python -m war_room_engine.daemon
```

See [`trading-war-room/README.md`](trading-war-room/README.md), `AGENTS.md`, `DECISIONS.md`, and `SCALP_PLAYBOOK.md`.
