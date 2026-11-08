# ENGINE_HANDOFF — paper-mode real-data rectification

Updated: 2026-11-08 (Asia/Calcutta)

## Fixed in this revision

1. **NO bets no longer price off the YES book.** Packets store `yes_token_id` + `no_token_id`, fetch the traded-side CLOB (or exact complement), enter at the traded **ask**, and compute TP/SL in that space (`TP > entry > SL` enforced).
2. **Exit cycle uses live marks.** `run-exit-cycle` pulls CLOB / DexScreener; if the live mark is unavailable it **skips** (never closes on invented/cached drift).
3. **Holder % fail-closed.** Pump.fun Stage 2 drops the mint when Solana RPC `getTokenLargestAccounts` fails — no synthetic 5%/35%.
4. **Vote field aliases.** `audit_score` / `narrative_score` / `quant_score` / `timing_score` accepted alongside canonical engine names.
5. **Invalid TP/SL gate.** `execute-paper` rejects packets where TP/SL are inverted vs entry.
6. **`repair-pending` CLI.** Reprices pending Polymarket packets from live books; rejects adverse/invalid ones.
7. **Optional live X.** When `XAI_API_KEY` is set, Polymarket sentiment calls xAI Live Search; otherwise proxies only (labeled `live_x=off`).

## Still requires operator secrets (paste in chat)

| Env var | Purpose |
|---|---|
| `XAI_API_KEY` | Live X/Grok velocity (optional but recommended) |
| `SOLANA_RPC_URL` | Reliable holder RPC (Helius/QuickNode/Alchemy) |
| `X_BEARER_TOKEN` | Optional direct X API (unused until wired; xAI preferred) |

Public Gamma / CLOB / DexScreener / PumpPortal need **no** keys.

## Paper-mode guarantee

`DRY_RUN=true`, `LIVE_TRADING_ENABLED=false`. No private keys loaded; no order placement code paths. Marks and books are **real public market data**.
