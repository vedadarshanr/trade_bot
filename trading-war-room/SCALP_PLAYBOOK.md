# SCALP_PLAYBOOK — Emotionless Scalp Execution Rules

We do **not** hold to binary resolution as a default. Both desks scalp catalyst/
imbalance moves with hard exits computed by Python (`hft_math.compute_scalp_targets`).
Bots never invent TP/SL levels in chat.

---

## Desk A — Polymarket Catalyst & Copy Scalping

### Entry (all must hold unless Safe-Compounder path)

1. Packet emitted by `scanners.py` after liquidity ≥ $10k, 24h volume ≥ $5k, spread ≤ $0.04.
2. At least one trigger:
   - Tracked Polymarket smart wallet (`SMART_WALLETS.md` Table A) entered in last **5 minutes**, OR
   - Order-book `imbalance >= 0.25` **and** `microprice` leads `mid_price` by ≥ **1.5¢**, OR
   - Safe Compounder: `NO` ask > $0.80, `YES` ≤ $0.20, edge > $0.05.
3. Stage-6 consensus ≥ 72 with Adversarial approve + Risk gates green.

### Exit (priority order)

| Priority | Rule | Default |
|---|---|---|
| 1 | Hard stop | `-3¢` (`stop_loss_cents`) |
| 2 | Imbalance reversal against us | imbalance `< -0.45` |
| 3 | Take profit | `+4¢` to `+12¢` (`take_profit_cents`, sized by volatility) |
| 4 | Dead catalyst time-stop | **30 minutes** if price move `< 1.5¢` |

Do not hold until market resolution for catalyst/copy packets.

---

## Desk B — Solana Pump.fun Momentum & Copy Scalping

### Entry filters

**Stage 1 (monitor):**

- Unique buyers ≥ **5**
- Bonding curve progress `< 40%`
- Age `> 120s`
- Resolved metadata (name/symbol/image via URI / IPFS / DexScreener)

**Stage 2 (analyzer):**

- `risk_score <= 7.0`
- Creator holding `< 25%`
- Top-5 holders `< 80%`

**Plus catalyst:** tracked Solana smart wallet buy **or** X ticker velocity spike
(`x_velocity_score` high / exhaustion not `HIGH`).

### Exit — 5-tier priority daemon

| Priority | Rule |
|---|---|
| 1 | Hard stop-loss **−12%** |
| 2 | Raydium bonding-curve migration event |
| 3 | Scalp take-profit: **+25%** partial, **+45%** full |
| 4 | Trailing stop **−10%** from peak |
| 5 | Max time-stop **12 minutes** if volume stalls |

`run-exit-cycle` evaluates open `paper_fills` against live marks and closes with
an explicit `exit_reason`.

---

## Shared Sizing

- Posterior probability from `bayesian_posterior` (smart-money likelihood × X velocity).
- Size from `quarter_kelly_scalp`, hard-capped at `MAX_POSITION_PCT = 0.03`.
- Packet fields `take_profit_price`, `stop_loss_price`, `max_hold_seconds` are authoritative.
