"""Live mark-to-market helpers (paper mode still uses real public books)."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .hft_math import calc_imbalance, calc_microprice, complement_book, mid_price

log = logging.getLogger("war_room.live_marks")

CLOB_BOOK = "https://clob.polymarket.com/book"
DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens"


def fetch_clob_top(
    token_id: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 6.0,
) -> Optional[tuple[float, float, float, float]]:
    """Return (bid, ask, q_bid, q_ask) from live CLOB, or None on failure."""
    owns = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        r = http.get(CLOB_BOOK, params={"token_id": token_id})
        if r.status_code >= 400:
            return None
        book = r.json()
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None
        bids_sorted = sorted(bids, key=lambda x: float(x.get("price", 0.0)), reverse=True)
        asks_sorted = sorted(asks, key=lambda x: float(x.get("price", 1.0)))
        bid = float(bids_sorted[0]["price"])
        q_bid = float(bids_sorted[0]["size"])
        ask = float(asks_sorted[0]["price"])
        q_ask = float(asks_sorted[0]["size"])
        if ask <= bid or bid <= 0:
            return None
        return bid, ask, q_bid, q_ask
    except Exception as exc:
        log.debug("CLOB book fetch failed for %s: %s", token_id[:12], exc)
        return None
    finally:
        if owns:
            http.close()


def mark_polymarket_fill(fill_row: Any, packet: dict[str, Any]) -> Optional[dict[str, float]]:
    """
    Live mark for an open Polymarket paper fill.

    Prefers the traded-side token id (yes_token_id / no_token_id). Falls back to
    complementing the YES book when only YES token is stored.
    """
    side = str(fill_row["side"] or packet.get("side") or "YES").upper()
    yes_tok = str(packet.get("yes_token_id") or packet.get("token_id") or "")
    no_tok = str(packet.get("no_token_id") or "")

    tob = None
    if side == "NO" and no_tok:
        tob = fetch_clob_top(no_tok)
    elif side != "NO" and yes_tok:
        tob = fetch_clob_top(yes_tok)
    elif yes_tok:
        tob = fetch_clob_top(yes_tok)
        if tob and side == "NO":
            tob = complement_book(*tob)
    elif no_tok:
        tob = fetch_clob_top(no_tok)
        if tob and side != "NO":
            tob = complement_book(*tob)

    if not tob:
        return None
    bid, ask, q_bid, q_ask = tob
    mid = mid_price(bid, ask)
    micro = calc_microprice(bid, ask, q_bid, q_ask)
    imb = calc_imbalance(q_bid, q_ask)
    # Mark at mid for MTM; entries still cross the ask.
    return {
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "microprice": micro,
        "imbalance": imb,
        "mark": mid,
    }


def mark_pumpfun_fill(mint: str) -> Optional[dict[str, float]]:
    """Live USD mark from DexScreener for a Pump.fun mint."""
    try:
        with httpx.Client(timeout=8.0) as http:
            r = http.get(f"{DEXSCREENER}/{mint}")
            if r.status_code >= 400:
                return None
            pairs = (r.json() or {}).get("pairs") or []
            if not pairs:
                return None
            p0 = pairs[0]
            price = float(p0.get("priceUsd") or 0.0)
            if price <= 0:
                return None
            return {"mark": price, "microprice": price, "imbalance": 0.0}
    except Exception as exc:
        log.debug("DexScreener mark failed for %s: %s", mint[:8], exc)
        return None
