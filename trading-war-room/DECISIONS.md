# DECISIONS — Active Mode & Hard Risk Boundaries

Machine-readable block below is the source of truth for `war_room_engine`.
Humans edit this file; bots and daemons parse the fenced JSON.

## Active Mode

```json
{
  "MODE": "PAPER",
  "DRY_RUN": true,
  "LIVE_TRADING_ENABLED": false,
  "PAPER_BANKROLL_USD": 10000.00
}
```

## Hard Quantitative Risk Boundaries

`hft.md` / `@zostaff` quarter-Kelly scalp rules. Paper and live use the same caps.

```json
{
  "KELLY_FRACTION": 0.25,
  "MAX_POSITION_PCT": 0.03,
  "DAILY_LOSS_LIMIT_PCT": 0.05,
  "DRAWDOWN_CIRCUIT_BREAKER_PCT": 0.15,
  "MAX_SECTOR_CONCENTRATION_PCT": 0.30,
  "MAX_OPEN_POSITIONS": 5,
  "MAX_DAILY_TRADES": 25,
  "MIN_STAGE6_SCORE": 72.0,
  "MAX_ADVERSE_IMBALANCE": -0.50
}
```

### Cap notes (on $10,000 paper bankroll)

| Knob | Meaning |
|---|---|
| `KELLY_FRACTION = 0.25` | Quarter-Kelly only |
| `MAX_POSITION_PCT = 0.03` | **$300** max notional per scalp |
| `DAILY_LOSS_LIMIT_PCT = 0.05` | Halt new entries after **-$500** day |
| `DRAWDOWN_CIRCUIT_BREAKER_PCT = 0.15` | Halt after **15%** peak-to-trough equity drawdown |
| `MAX_SECTOR_CONCENTRATION_PCT = 0.30` | ≤ **30%** exposure per narrative/category |
| `MAX_OPEN_POSITIONS = 5` | Concurrent open paper fills |
| `MAX_DAILY_TRADES = 25` | High-turnover scalp ceiling |
| `MIN_STAGE6_SCORE = 72.0` | Consensus floor before Risk-Execution |
| `MAX_ADVERSE_IMBALANCE = -0.50` | Reject / force-exit when imbalance ≤ this |

## Six Risk Gates (Risk-Execution-Bot)

1. Stage-6 score ≥ `MIN_STAGE6_SCORE`
2. Adversarial approve == true
3. Open positions < `MAX_OPEN_POSITIONS`
4. Daily trade count < `MAX_DAILY_TRADES`
5. Realized day PnL > `-DAILY_LOSS_LIMIT_PCT * bankroll`
6. Peak-to-trough drawdown < `DRAWDOWN_CIRCUIT_BREAKER_PCT` **and** sector exposure ≤ `MAX_SECTOR_CONCENTRATION_PCT` **and** sized notional ≤ `MAX_POSITION_PCT * bankroll` **and** entry imbalance > `MAX_ADVERSE_IMBALANCE`

Gate 6 is the compound portfolio/microstructure gate. Any failure ⇒ deny fill.

## Blacklists (JSON)

```json
{
  "BLACKLISTED_SOLANA_DEPLOYERS": [],
  "VETOED_POLYMARKET_CONDITIONS": []
}
```

Append deployer base58 addresses and Polymarket `conditionId` / slug strings as they are identified. Empty lists are valid at boot.
