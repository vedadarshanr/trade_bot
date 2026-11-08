# AGENTS — 6-Seat Cognitive Factor Architecture

This war room separates **deterministic Python ingestion/math** from **Grok bot judgment**.
Bots never scrape, never poll prices in chat loops, and never invent exit prices by hand.

## Golden Rule

1. Python daemons compute all metrics and append decision packets to `state/PIPELINE-EVIDENCE.jsonl` (and `state/war_room.db` → `candidates`).
2. Bot seats **only** read a pre-computed packet and submit structured JSON via:

```bash
python3 war_room_engine/cli_vote.py vote --packet <PACKET_ID> --seat <SEAT_NAME> --json '<JSON_PAYLOAD>'
```

3. Fail-closed: any parse error, missing field, timeout, or schema mismatch ⇒ `"approve": false` / score `0`.

## Seats

| Seat | Role | Input | Output (JSON) |
|---|---|---|---|
| `Chief-Dispatcher` | Triage packet desk/urgency; assign review order | packet | `{"seat":"Chief-Dispatcher","priority":0-100,"desk":"polymarket\|pumpfun","notes":"..."}` |
| `OnChain-Auditor` | Audit copy/smart-money quality + deployer/holder risk | packet | `{"seat":"OnChain-Auditor","audit_and_copy_score":0-100,"flags":[],"approve":true\|false}` |
| `Narrative-Scout` | Score X narrative velocity + macro timing | packet | `{"seat":"Narrative-Scout","x_narrative_velocity_score":0-100,"macro_timing_score":0-100,"exhaustion_risk":"LOW\|MEDIUM\|HIGH"}` |
| `Quant-Pricer` | Validate microstructure packet (microprice/imbalance/Kelly) | packet | `{"seat":"Quant-Pricer","microstructure_quant_score":0-100,"kelly_pct":0-0.03,"edge_cents":number}` |
| `Adversarial-Checker` | Hunt reasons **not** to trade; never invent bull cases | packet + prior votes | `{"seat":"Adversarial-Checker","approve":true\|false,"veto_reasons":[],"confidence":0-1}` |
| `Risk-Execution-Bot` | Enforce `DECISIONS.md` gates; open/close paper fills | consensus + portfolio | calls `execute-paper` / `run-exit-cycle` only |

## Stage 6 Consensus Formula (LOCKED)

```
Total_Score =
  (0.30 * Audit_And_Copy_Score) +
  (0.25 * X_Narrative_Velocity_Score) +
  (0.15 * Macro_Timing_Score) +
  (0.30 * Microstructure_Quant_Score)
```

Scores are `0–100`. `cli_vote.py` computes `Total_Score` automatically once
`OnChain-Auditor`, `Narrative-Scout`, and `Quant-Pricer` have voted.

## Execution Gate (LOCKED)

A paper or live scalp may open **only if all** are true:

1. `Total_Score >= 72.0`
2. `Adversarial-Checker.approve == true`
3. `Risk-Execution-Bot` passes all **6** portfolio risk gates in `DECISIONS.md`

Otherwise: no fill. Default deny.

## Bot Workflow

```bash
python3 war_room_engine/cli_vote.py list-pending
python3 war_room_engine/cli_vote.py vote --packet <ID> --seat OnChain-Auditor --json '...'
python3 war_room_engine/cli_vote.py vote --packet <ID> --seat Narrative-Scout --json '...'
python3 war_room_engine/cli_vote.py vote --packet <ID> --seat Quant-Pricer --json '...'
python3 war_room_engine/cli_vote.py vote --packet <ID> --seat Adversarial-Checker --json '...'
python3 war_room_engine/cli_vote.py execute-paper --packet <ID>
python3 war_room_engine/cli_vote.py run-exit-cycle
```

## What Bots Must Never Do

- Write scraper / websocket / REST polling code in chat.
- Recompute microprice, imbalance, Kelly, or Stage-2 risk by hand.
- Edit `state/war_room.db` or `PIPELINE-EVIDENCE.jsonl` directly.
- Override `take_profit_price` / `stop_loss_price` / `max_hold_seconds` from the packet.
