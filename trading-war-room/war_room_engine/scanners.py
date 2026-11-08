#!/usr/bin/env python3
"""
Unified Solana Pump.fun WebSocket Scanner (Stage 1 -> 1.5 -> 2 Funnel + IPFS/DexScreener
Metadata Resolver) and Polymarket CLOB Pre-Filter & Packet Emitter.

Imports war_room_engine.db / hft_math / x_sentiment / copy_tracker when present;
keeps built-in math fallbacks so ImportError never crashes the pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore

logger = logging.getLogger("war_room.scanners")

try:
    from war_room_engine.copy_tracker import CopyTracker, SmartMoneyCopyTracker
    from war_room_engine.db import Database, get_db, utcnow
    from war_room_engine.decisions import load_decisions
    from war_room_engine.hft_math import (
        bayesian_posterior as _hft_bayes,
        calc_imbalance as _hft_imb,
        calc_microprice as _hft_micro,
        complement_book,
        compute_scalp_targets,
        mid_price as _hft_mid,
        quarter_kelly_scalp as _hft_kelly,
        validate_long_targets,
    )
    from war_room_engine.paths import DB_PATH, EVIDENCE_PATH, STATE
    from war_room_engine.x_sentiment import (
        XSentimentScorer,
        extract_text_signals,
        has_social_metadata,
        score_velocity,
    )

    def calc_microprice(bid: float, ask: float, q_bid: float, q_ask: float) -> float:
        return round(_hft_micro(bid, ask, q_bid, q_ask), 8)

    def calc_imbalance(q_bid: float, q_ask: float) -> float:
        return round(_hft_imb(q_bid, q_ask), 4)

    def bayesian_posterior(prior: float, likelihood_pos: float, likelihood_neg: float) -> float:
        return round(_hft_bayes(prior, likelihood_pos, likelihood_neg), 4)

    def mid_price(bid: float, ask: float) -> float:
        return _hft_mid(bid, ask)

    def quarter_kelly_scalp(
        posterior_p: float,
        payout_odds_b: float | None = None,
        stop_loss_frac_a: float = 1.0,
        *,
        take_profit_pct: float | None = None,
        stop_loss_pct: float | None = None,
    ) -> float:
        if take_profit_pct is not None and stop_loss_pct is not None:
            return round(_hft_kelly(posterior_p, take_profit_pct, stop_loss_pct), 4)
        # Rails signature: payout odds b / stop fraction a
        b = float(payout_odds_b or 0.0)
        a = float(stop_loss_frac_a or 1.0)
        if b <= 0 or a <= 0:
            return 0.0
        f_star = (posterior_p / a) - ((1.0 - posterior_p) / b)
        return round(min(0.03, max(0.0, 0.25 * f_star)), 4)

except ImportError:  # pragma: no cover
    ROOT = Path(os.path.expanduser("~/workspace/trading-war-room"))
    STATE = ROOT / "state"
    DB_PATH = STATE / "war_room.db"
    EVIDENCE_PATH = STATE / "PIPELINE-EVIDENCE.jsonl"
    Database = None  # type: ignore
    CopyTracker = None  # type: ignore
    SmartMoneyCopyTracker = None  # type: ignore
    XSentimentScorer = None  # type: ignore
    compute_scalp_targets = None  # type: ignore

    def get_db():  # type: ignore
        raise RuntimeError("db unavailable")

    def utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()

    def load_decisions():  # type: ignore
        class _D:
            vetoed_polymarket_conditions: set = set()
            blacklisted_solana_deployers: set = set()

        return _D()

    def calc_microprice(bid: float, ask: float, q_bid: float, q_ask: float) -> float:
        denom = max(q_bid + q_ask, 1e-9)
        return round((bid * q_ask + ask * q_bid) / denom, 8)

    def calc_imbalance(q_bid: float, q_ask: float) -> float:
        denom = max(q_bid + q_ask, 1e-9)
        return round((q_bid - q_ask) / denom, 4)

    def bayesian_posterior(prior: float, likelihood_pos: float, likelihood_neg: float) -> float:
        prior = max(0.01, min(0.99, prior))
        num = likelihood_pos * prior
        den = num + (likelihood_neg * (1.0 - prior))
        return round(num / max(den, 1e-9), 4)

    def mid_price(bid: float, ask: float) -> float:
        return (bid + ask) / 2.0

    def complement_book(
        bid: float, ask: float, q_bid: float, q_ask: float
    ) -> tuple[float, float, float, float]:
        no_bid = max(0.0, min(1.0, 1.0 - ask))
        no_ask = max(0.0, min(1.0, 1.0 - bid))
        return round(no_bid, 6), round(no_ask, 6), q_ask, q_bid

    def validate_long_targets(entry: float, tp: float, sl: float) -> tuple[float, float]:
        if tp <= entry:
            tp = min(0.99, entry + 0.04)
        if sl >= entry:
            sl = max(0.001, entry - 0.03)
        return round(tp, 6), round(sl, 6)

    def quarter_kelly_scalp(
        posterior_p: float,
        payout_odds_b: float | None = None,
        stop_loss_frac_a: float = 1.0,
        *,
        take_profit_pct: float | None = None,
        stop_loss_pct: float | None = None,
    ) -> float:
        if take_profit_pct is not None and stop_loss_pct is not None:
            p = max(1e-9, min(1 - 1e-9, posterior_p))
            tp = max(1e-9, abs(take_profit_pct))
            sl = max(1e-9, abs(stop_loss_pct))
            f_star = (p / sl) - ((1.0 - p) / tp)
            return round(min(0.03, max(0.0, 0.25 * f_star)), 4)
        b = float(payout_odds_b or 0.0)
        a = float(stop_loss_frac_a or 1.0)
        if b <= 0 or a <= 0:
            return 0.0
        f_star = (posterior_p / a) - ((1.0 - posterior_p) / b)
        return round(min(0.03, max(0.0, 0.25 * f_star)), 4)

    def extract_text_signals(meta: dict[str, Any]) -> str:
        return " ".join(str(meta.get(k) or "") for k in ("name", "symbol", "description", "question"))

    def has_social_metadata(meta: dict[str, Any]) -> bool:
        return "twitter" in json.dumps(meta).lower() or "x.com" in json.dumps(meta).lower()

    def score_velocity(**kwargs: Any) -> Any:
        class _R:
            x_velocity_score = 50.0
            exhaustion_risk = "LOW"
            macro_timing_score = 50.0

        return _R()


GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens"
PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"
try:
    from war_room_engine.secrets import get_secret as _get_secret

    SOLANA_PUBLIC_RPC = _get_secret(
        "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
    )
except Exception:
    SOLANA_PUBLIC_RPC = os.getenv(
        "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
    )

IPFS_GATEWAYS = [
    "https://ipfs.io/ipfs/",
    "https://cf-ipfs.com/ipfs/",
    "https://gateway.pinata.cloud/ipfs/",
    "https://dweb.link/ipfs/",
]

CATEGORY_SCORES: Dict[str, float] = {
    "crypto": 82.0,
    "politics": 78.0,
    "tech": 79.0,
    "business": 75.0,
    "world": 74.0,
    "science": 76.0,
    "economics": 66.0,
    "sports": 45.0,
    "entertainment": 48.0,
    "pop-culture": 48.0,
}

PACKET_RATE_WINDOW = 300.0
MAX_PACKETS_PER_WINDOW = 2
DEDUP_COOLDOWN = 2 * 3600


def im_positive(imbalance: float) -> bool:
    return imbalance >= 0.0


@dataclass
class PendingPumpToken:
    mint: str
    created_at: float
    creator: str = ""
    symbol: str = ""
    name: str = ""
    uri: str = ""
    bonding_curve_key: str = ""
    buyers: set[str] = field(default_factory=set)
    sol_in_curve: float = 0.0
    bonding_curve_pct: float = 0.0
    sniper_count: int = 0
    wallet_trade_counts: dict[str, int] = field(default_factory=dict)
    ping_pong_wallets: int = 0
    trades: list[dict[str, Any]] = field(default_factory=list)
    last_price: float = 0.000030
    raw_payload: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    evaluated: bool = False


class PacketEmitter:
    """
    At most `max_packets_per_5m` packets / 5 minutes into PIPELINE-EVIDENCE.jsonl,
    with a 2-hour per-asset cooldown (memory + SQLite).
    """

    def __init__(
        self,
        db: Any = None,
        evidence_path: Path | None = None,
        db_path: Path | None = None,
        jsonl_path: Path | None = None,
        max_packets_per_5m: int = MAX_PACKETS_PER_WINDOW,
        cooldown_seconds: int = DEDUP_COOLDOWN,
    ) -> None:
        if db is not None:
            self.db = db
        else:
            try:
                self.db = get_db()
            except Exception:
                self.db = None
        self.evidence_path = Path(evidence_path or jsonl_path or EVIDENCE_PATH)
        self.evidence_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_packets_per_5m = max_packets_per_5m
        self.cooldown_seconds = cooldown_seconds
        self._emitted_at: dict[str, float] = {}
        self._recent_emits: deque[float] = deque()
        self._emit_timestamps: List[float] = []
        self._recent_assets: Dict[str, float] = {}
        self._hydrate_cooldown_from_db()

    def _hydrate_cooldown_from_db(self) -> None:
        if self.db is None:
            return
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    "SELECT asset_id, created_at FROM candidates "
                    "WHERE created_at >= datetime('now', '-4 hours')"
                )
                now = time.time()
                for row in cur.fetchall():
                    asset_id = str(row["asset_id"] or "")
                    if not asset_id:
                        continue
                    try:
                        ts = datetime.fromisoformat(str(row["created_at"])).timestamp()
                    except ValueError:
                        ts = now
                    self._recent_assets[asset_id] = ts
                    self._emitted_at[asset_id] = ts
        except Exception:
            pass

    def _rate_ok(self) -> bool:
        now = time.time()
        while self._recent_emits and now - self._recent_emits[0] > PACKET_RATE_WINDOW:
            self._recent_emits.popleft()
        self._emit_timestamps = [t for t in self._emit_timestamps if (now - t) < 300.0]
        return (
            len(self._recent_emits) < self.max_packets_per_5m
            and len(self._emit_timestamps) < self.max_packets_per_5m
        )

    def _cooldown_ok(self, asset_id: str) -> bool:
        now = time.time()
        last = self._emitted_at.get(asset_id) or self._recent_assets.get(asset_id)
        if last and (now - last) < self.cooldown_seconds:
            return False
        if self.db is not None:
            try:
                with self.db.cursor() as cur:
                    cur.execute(
                        "SELECT created_at FROM candidates WHERE asset_id=? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (asset_id,),
                    )
                    row = cur.fetchone()
                if row and row["created_at"]:
                    ts = datetime.fromisoformat(str(row["created_at"])).timestamp()
                    if now - ts < self.cooldown_seconds:
                        self._emitted_at[asset_id] = ts
                        self._recent_assets[asset_id] = ts
                        return False
            except Exception:
                pass
        return True

    def can_emit(self, asset_id: str) -> bool:
        if not asset_id:
            return False
        return self._rate_ok() and self._cooldown_ok(asset_id)

    def _normalize_packet(self, packet: Dict[str, Any]) -> Dict[str, Any]:
        """Map rails desk names + ensure cli_vote / db fields exist."""
        desk = str(packet.get("desk") or "")
        desk_map = {
            "POLYMARKET_CLOB": "polymarket",
            "SOLANA_PUMPFUN": "pumpfun",
            "polymarket": "polymarket",
            "pumpfun": "pumpfun",
        }
        packet["desk"] = desk_map.get(desk, desk.lower() or "unknown")
        if not packet.get("asset_id"):
            packet["asset_id"] = str(
                packet.get("mint") or packet.get("condition_id") or ""
            )
        if not packet.get("ticker_or_slug"):
            packet["ticker_or_slug"] = str(
                packet.get("ticker")
                or packet.get("market_slug")
                or packet.get("slug")
                or packet.get("asset_id")
                or ""
            )[:80]
        if "smart_money_signal" not in packet:
            packet["smart_money_signal"] = bool(packet.get("smart_money_hits"))
        if "stage2_metrics_score" not in packet:
            packet["stage2_metrics_score"] = float(packet.get("metrics_score") or 0.0)
        if "kelly_pct" not in packet and packet.get("quarter_kelly_pct") is not None:
            packet["kelly_pct"] = packet["quarter_kelly_pct"]
        if "macro_timing_score" not in packet and packet.get("timing_score") is not None:
            packet["macro_timing_score"] = packet["timing_score"]
        if "entry_price" not in packet:
            packet["entry_price"] = packet.get("ask") or packet.get("mid_price") or packet.get("microprice")
        packet.setdefault("packet_id", f"pkt_{uuid.uuid4().hex[:16]}")
        packet.setdefault("created_at", utcnow())
        packet.setdefault("status", "pending")
        return packet

    def emit(self, packet: dict[str, Any]) -> bool:
        packet = self._normalize_packet(dict(packet))
        asset_id = str(packet.get("asset_id") or "")
        if not asset_id or not self.can_emit(asset_id):
            return False

        try:
            decisions = load_decisions()
            if (
                packet.get("desk") == "polymarket"
                and asset_id in getattr(decisions, "vetoed_polymarket_conditions", set())
            ):
                return False
            creator = packet.get("creator") or packet.get("creator_wallet")
            if (
                packet.get("desk") == "pumpfun"
                and creator
                and creator in getattr(decisions, "blacklisted_solana_deployers", set())
            ):
                return False
        except Exception:
            pass

        now = time.time()
        line = json.dumps(packet, ensure_ascii=False, default=str)
        with self.evidence_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

        if self.db is not None:
            try:
                self.db.upsert_candidate(packet)
            except Exception as exc:
                logger.warning("upsert_candidate failed: %s", exc)

        self._emitted_at[asset_id] = now
        self._recent_assets[asset_id] = now
        self._recent_emits.append(now)
        self._emit_timestamps.append(now)
        logger.info(
            "EMITTED PACKET %s [%s] asset=%s metrics_score=%.1f",
            packet["packet_id"],
            packet.get("desk"),
            asset_id[:16],
            float(packet.get("stage2_metrics_score") or 0.0),
        )
        return True

    def emit_packet(self, packet: Dict[str, Any]) -> bool:
        return self.emit(packet)


class PolymarketScanner:
    """
    Scans active Polymarket order books, filters to Mode A (Safe Compounder),
    Mode B (imbalance/smart-money scalp), emits ≤2 packets / 5 minutes.
    """

    def __init__(
        self,
        emitter: PacketEmitter | None = None,
        tracker: Any = None,
        http: Any = None,
        copy_tracker: Any = None,
        sentiment_scorer: Any = None,
    ) -> None:
        # Rails positional: PolymarketScanner(copy_tracker, sentiment_scorer, emitter)
        if (
            emitter is not None
            and not isinstance(emitter, PacketEmitter)
            and isinstance(http, PacketEmitter)
        ):
            self.tracker = emitter
            self.sentiment_scorer = tracker or (
                XSentimentScorer() if XSentimentScorer else None
            )
            self.emitter = http
            self.http = None
        else:
            self.emitter = (
                emitter if isinstance(emitter, PacketEmitter) else PacketEmitter()
            )
            self.tracker = tracker or copy_tracker
            if sentiment_scorer is not None:
                self.sentiment_scorer = sentiment_scorer
            elif tracker is not None and hasattr(tracker, "score_polymarket_candidate"):
                self.sentiment_scorer = tracker
                self.tracker = copy_tracker
            else:
                self.sentiment_scorer = (
                    XSentimentScorer() if XSentimentScorer else None
                )
            self.http = http if not isinstance(http, PacketEmitter) else None
        self.last_markets_scanned: int = 0
        self.last_candidates_qualified: int = 0

    async def scan_once(self, session: Any = None) -> int:
        http = session or self.http
        if http is None:
            if httpx is None:
                logger.warning("no HTTP client for PolymarketScanner")
                return 0
            async with httpx.AsyncClient(timeout=25.0) as client:
                return await self._scan_with(client)
        return await self._scan_with(http)

    async def _scan_with(self, http: Any) -> int:
        markets: List[Dict[str, Any]] = []
        for offset in (0, 200, 400):
            batch = await self._fetch_markets(http, offset)
            if not batch:
                break
            markets.extend(batch)
            if len(batch) < 200:
                break

        self.last_markets_scanned = len(markets)
        scored_candidates: List[Tuple[float, Dict[str, Any]]] = []

        for m in markets:
            if not isinstance(m, dict):
                continue
            packet = await self._evaluate_market(http, m)
            if packet:
                scored_candidates.append(
                    (
                        float(packet.get("stage2_metrics_score") or 0)
                        + float(packet.get("x_velocity_score") or 0),
                        packet,
                    )
                )

        scored_candidates.sort(key=lambda x: x[0], reverse=True)
        self.last_candidates_qualified = len(scored_candidates)
        emitted = 0
        for _, pkt in scored_candidates:
            if self.emitter.emit(pkt):
                emitted += 1
            if emitted >= self.emitter.max_packets_per_5m:
                break
        return emitted

    async def _fetch_markets(self, http: Any, offset: int) -> list:
        try:
            if hasattr(http, "get") and "aiohttp" in type(http).__module__:
                async with http.get(
                    f"{GAMMA_MARKETS_URL}?active=true&closed=false&limit=200&offset={offset}",
                    timeout=8,
                ) as resp:
                    if resp.status != 200:
                        return []
                    batch = await resp.json()
                    return batch if isinstance(batch, list) else []
            r = await http.get(
                GAMMA_MARKETS_URL,
                params={"active": "true", "closed": "false", "limit": 200, "offset": offset},
            )
            r.raise_for_status()
            batch = r.json()
            return batch if isinstance(batch, list) else []
        except Exception as exc:
            logger.debug("Gamma markets fetch offset=%d warning: %s", offset, exc)
            return []

    async def _evaluate_market(self, http: Any, m: dict[str, Any]) -> Optional[dict[str, Any]]:
        if m.get("active") is False or m.get("closed") or m.get("enableOrderBook") is False:
            # Still allow when enableOrderBook missing (gamma often omits it).
            if m.get("closed") or m.get("active") is False:
                return None

        liquidity = float(m.get("liquidityNum") or m.get("liquidity") or 0.0)
        volume_24hr = float(m.get("volume24hr") or m.get("volumeNum") or 0.0)
        if liquidity < 10_000.0 or volume_24hr < 5_000.0:
            return None

        condition_id = str(m.get("conditionId") or m.get("id") or "").strip()
        if not condition_id or not self.emitter.can_emit(condition_id):
            return None

        clob_tokens = self._parse_json_field(m.get("clobTokenIds"))
        outcome_prices = self._parse_json_field(m.get("outcomePrices"))
        if len(clob_tokens) < 1:
            return None

        yes_token_id = str(clob_tokens[0])
        no_token_id = str(clob_tokens[1]) if len(clob_tokens) > 1 else ""
        yes_mid_hint = float(outcome_prices[0]) if outcome_prices else 0.50
        slug = str(m.get("slug") or condition_id[:16])
        question = str(m.get("question") or slug)
        category = str(m.get("category") or "Crypto").strip()
        cat_key = category.lower()
        category_score = CATEGORY_SCORES.get(cat_key, 72.0)

        sm_hits, sm_score, sm_side, sm_aliases = 0, 0.0, "NONE", []
        hits: list = []
        if self.tracker is not None:
            if hasattr(self.tracker, "get_asset_smart_money_summary"):
                sm_hits, sm_score, sm_side, sm_aliases = (
                    self.tracker.get_asset_smart_money_summary(condition_id, slug)
                )
            if hasattr(self.tracker, "hits_for_asset"):
                hits = self.tracker.hits_for_asset(condition_id, within_seconds=300)
                if slug:
                    hits = hits + self.tracker.hits_for_asset(slug, within_seconds=300)
                sm_hits = max(sm_hits, len({h.wallet for h in hits}))
                if hits and not sm_aliases:
                    sm_aliases = [h.wallet for h in hits]
                    sm_side = hits[-1].side

        is_potential_compounder = (
            yes_mid_hint <= 0.20
            and cat_key not in ("sports", "entertainment", "pop-culture")
            and "mention" not in question.lower()
            and "say " not in question.lower()
        )
        is_potential_directional = 0.15 <= yes_mid_hint <= 0.85 and category_score >= 65.0
        legacy_safe_hint = yes_mid_hint <= 0.20

        if not (sm_hits > 0 or is_potential_compounder or is_potential_directional or legacy_safe_hint):
            return None

        yes_tob = await self.fetch_top_of_book(http, yes_token_id)
        if not yes_tob:
            return None
        yes_bid, yes_ask, yes_q_bid, yes_q_ask = yes_tob
        yes_spread = round(yes_ask - yes_bid, 4)
        if yes_spread > 0.04 or yes_ask <= yes_bid:
            return None

        yes_mid = round(mid_price(yes_bid, yes_ask), 4)
        yes_micro = round(calc_microprice(yes_bid, yes_ask, yes_q_bid, yes_q_ask), 4)
        yes_imb = calc_imbalance(yes_q_bid, yes_q_ask)
        yes_edge = round(yes_micro - yes_mid, 4)

        # Prefer live NO book when it is actually the complementary token.
        # If the response looks like another YES-priced book, use the exact complement.
        no_from_complement = complement_book(yes_bid, yes_ask, yes_q_bid, yes_q_ask)
        no_tob = None
        if no_token_id:
            live_no = await self.fetch_top_of_book(http, no_token_id)
            if live_no is not None:
                live_mid = mid_price(live_no[0], live_no[1])
                if yes_mid <= 0.55 and live_mid < 0.45:
                    no_tob = no_from_complement
                else:
                    no_tob = live_no
        if no_tob is None:
            no_tob = no_from_complement
        no_bid, no_ask, no_q_bid, no_q_ask = no_tob
        no_spread = round(no_ask - no_bid, 4)

        mode_a_qualified = (
            (is_potential_compounder or legacy_safe_hint)
            and no_ask > 0.80
            and yes_ask <= 0.20
        )
        mode_b_qualified = (
            is_potential_directional
            and (abs(yes_imb) >= 0.25 or sm_hits > 0)
            and (abs(yes_edge) >= 0.008 or sm_hits > 0)
        )
        orderbook_trigger = abs(yes_imb) >= 0.28 and abs(yes_micro - yes_mid) >= 0.015

        if sm_hits > 0:
            trigger = "smart_money_entry"
            mode = "SCALP_DIRECTIONAL"
            # Follow smart-money side when available; else YES-imbalance direction.
            side = str(sm_side or "").upper()
            if side not in ("YES", "NO"):
                side = "YES" if yes_imb >= 0 else "NO"
        elif mode_a_qualified:
            trigger = "safe_compounder"
            mode = "SAFE_COMPOUNDER"
            side = "NO"
        elif mode_b_qualified or orderbook_trigger:
            trigger = "orderbook_imbalance"
            mode = "SCALP_DIRECTIONAL"
            side = "YES" if yes_imb >= 0 else "NO"
        else:
            return None

        # Price EVERYTHING in the traded-side book (never YES mids for a NO trade).
        if side == "NO":
            bid, ask, q_bid, q_ask = no_bid, no_ask, no_q_bid, no_q_ask
            spread = no_spread
            book_source = "NO_TOKEN" if no_token_id and no_tob else "YES_COMPLEMENT"
        else:
            bid, ask, q_bid, q_ask = yes_bid, yes_ask, yes_q_bid, yes_q_ask
            spread = yes_spread
            book_source = "YES_TOKEN"

        if spread > 0.04 or ask <= bid or ask <= 0:
            return None

        mid = round(mid_price(bid, ask), 4)
        microprice = round(calc_microprice(bid, ask, q_bid, q_ask), 4)
        imbalance = calc_imbalance(q_bid, q_ask)
        microprice_edge = round(microprice - mid, 4)
        # Cross the ask to enter a long on the traded token.
        entry = float(ask)

        if self.sentiment_scorer is not None and hasattr(
            self.sentiment_scorer, "score_polymarket_candidate"
        ):
            sentiment = await self.sentiment_scorer.score_polymarket_candidate(
                session=http,
                question=question,
                slug=slug,
                category=category,
                volume_24hr=volume_24hr,
                liquidity=liquidity,
                imbalance=imbalance,
                microprice_edge=microprice_edge,
                mid_price=mid,
                smart_money_hits=sm_hits,
                smart_money_side=sm_side,
            )
            x_vel = sentiment.x_velocity_score
            timing = sentiment.timing_score
            exhaustion = sentiment.exhaustion_risk
            lp = sentiment.likelihood_pos
            ln = sentiment.likelihood_neg
            narrative = sentiment.narrative_hint
        else:
            text = extract_text_signals(m)
            sr = score_velocity(
                mention_acceleration=0.4 if sm_hits else 0.15,
                social_links_present=False,
                text_blob=text,
                smart_money_convergence=sm_hits,
                price_move_pct=abs(microprice_edge),
                catalyst_started_at=time.time() - (60 if sm_hits else 300),
            )
            x_vel = sr.x_velocity_score
            timing = sr.macro_timing_score
            exhaustion = sr.exhaustion_risk
            lp = 0.70 if sm_hits else 0.55
            ln = 0.45 if x_vel >= 60 else 0.55
            narrative = text[:120]

        prior_p = mid
        posterior_p = bayesian_posterior(prior_p, lp, ln)

        if compute_scalp_targets is not None:
            vol = max(0.01, spread * 2)
            targets = compute_scalp_targets(
                "polymarket", entry, microprice, imbalance, vol, side=side
            )
            kelly = quarter_kelly_scalp(
                posterior_p,
                take_profit_pct=targets.take_profit_pct,
                stop_loss_pct=targets.stop_loss_pct,
            )
            tp, sl = validate_long_targets(
                entry, targets.take_profit_price, targets.stop_loss_price
            )
            hold = targets.time_stop_seconds
            trail = targets.trailing_stop_pct
        else:
            payout_odds_b = round((1.0 - ask) / max(ask, 0.02), 3)
            kelly = quarter_kelly_scalp(posterior_p, payout_odds_b=payout_odds_b)
            tp, sl = validate_long_targets(entry, ask + 0.06, bid - 0.03)
            hold = 1800
            trail = 0.02

        # Veto packets whose book leans against the intended long.
        if imbalance <= -0.45:
            return None

        metrics_score = round(
            min(
                98.0,
                max(
                    55.0,
                    category_score * 0.55
                    + abs(imbalance) * 32.0
                    + (14.0 if sm_hits > 0 else 0.0)
                    + max(0.0, (0.04 - spread) * 300.0),
                ),
            ),
            2,
        )
        if trigger == "safe_compounder":
            metrics_score = max(
                metrics_score,
                min(
                    100.0,
                    40.0
                    + 20.0 * min(1.0, liquidity / 50_000)
                    + 20.0 * min(1.0, volume_24hr / 25_000)
                    + 20.0 * max(0.0, 1.0 - spread / 0.04),
                ),
            )

        return {
            "packet_id": f"pkt_poly_{uuid.uuid4().hex[:8]}",
            "desk": "polymarket",
            "asset_id": condition_id,
            "condition_id": condition_id,
            "yes_token_id": yes_token_id,
            "no_token_id": no_token_id,
            "ticker_or_slug": slug,
            "market_slug": slug,
            "question": question,
            "category": category,
            "category_score": category_score,
            "mode": mode,
            "trigger": trigger,
            "book_source": book_source,
            "smart_money_signal": sm_hits > 0,
            "smart_money_hits": sm_hits,
            "smart_money_max_score": sm_score,
            "smart_money_wallets": sm_aliases,
            "x_velocity_score": x_vel,
            "timing_score": timing,
            "macro_timing_score": timing,
            "exhaustion_risk": exhaustion,
            "narrative_hint": narrative,
            "stage2_metrics_score": round(metrics_score, 2),
            "metrics_score": round(metrics_score, 2),
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "bid": bid,
            "ask": ask,
            "q_bid": q_bid,
            "q_ask": q_ask,
            "microprice": microprice,
            "imbalance": imbalance,
            "microstructure": {
                "bid": bid,
                "ask": ask,
                "q_bid": q_bid,
                "q_ask": q_ask,
                "mid_price": mid,
                "microprice": microprice,
                "imbalance": imbalance,
                "edge_cents": microprice_edge * 100.0,
                "spread": spread,
                "book_source": book_source,
                "yes_mid": yes_mid,
                "yes_imbalance": yes_imb,
            },
            "prior_win_prob": prior_p,
            "likelihood_pos": lp,
            "likelihood_neg": ln,
            "posterior_prob": posterior_p,
            "bayesian_posterior": posterior_p,
            "quarter_kelly_pct": kelly,
            "kelly_pct": kelly,
            "side": side,
            "entry_price": entry,
            "take_profit_price": tp,
            "stop_loss_price": sl,
            "trailing_stop_pct": trail if compute_scalp_targets else 0.02,
            "max_hold_seconds": hold,
            "book_depth_usd": round(liquidity, 2),
            "volume_24hr_usd": round(volume_24hr, 2),
            "liquidity": liquidity,
            "volume_24h": volume_24hr,
            "status": "pending",
            "created_at": utcnow(),
        }

    async def fetch_top_of_book(
        self, session: Any, token_id: str
    ) -> Optional[Tuple[float, float, float, float]]:
        try:
            if hasattr(session, "get") and "aiohttp" in type(session).__module__:
                async with session.get(
                    f"{CLOB_BOOK_URL}?token_id={token_id}", timeout=4.5
                ) as resp:
                    if resp.status != 200:
                        return None
                    book = await resp.json()
            else:
                r = await session.get(CLOB_BOOK_URL, params={"token_id": token_id})
                if r.status_code >= 400:
                    return None
                book = r.json()
        except Exception:
            return None

        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None
        try:
            bids_sorted = sorted(bids, key=lambda x: float(x.get("price", 0.0)), reverse=True)
            asks_sorted = sorted(asks, key=lambda x: float(x.get("price", 1.0)))
            best_bid = float(bids_sorted[0]["price"])
            q_bid = float(bids_sorted[0]["size"])
            best_ask = float(asks_sorted[0]["price"])
            q_ask = float(asks_sorted[0]["size"])
        except (TypeError, ValueError, KeyError, IndexError):
            return None
        if best_ask <= best_bid or best_bid <= 0.0:
            return None
        return best_bid, best_ask, q_bid, q_ask

    @staticmethod
    def _parse_json_field(val: Any) -> List[Any]:
        if isinstance(val, list):
            return val
        if isinstance(val, str) and val.strip().startswith("["):
            try:
                return json.loads(val)
            except Exception:
                return []
        return []


class PumpFunScanner:
    """
    PumpPortal WS with keepalive + backoff, IPFS/DexScreener metadata resolve,
    120s incubation, Stage 1 → 1.5 → 2 funnel.
    """

    def __init__(
        self,
        emitter: PacketEmitter | None = None,
        tracker: Any = None,
        http: Any = None,
        copy_tracker: Any = None,
        sentiment_scorer: Any = None,
    ) -> None:
        # Rails positional: PumpFunScanner(copy_tracker, sentiment_scorer, emitter)
        if (
            emitter is not None
            and not isinstance(emitter, PacketEmitter)
            and isinstance(http, PacketEmitter)
        ):
            self.tracker = emitter
            self.sentiment_scorer = tracker or (
                XSentimentScorer() if XSentimentScorer else None
            )
            self.emitter = http
            self.http = None
        else:
            self.emitter = emitter if isinstance(emitter, PacketEmitter) else PacketEmitter()
            self.tracker = tracker or copy_tracker
            self.sentiment_scorer = sentiment_scorer or (
                XSentimentScorer() if XSentimentScorer else None
            )
            self.http = http if not isinstance(http, PacketEmitter) else None

        self.incubating_tokens: Dict[str, PendingPumpToken] = {}
        self.pending = self.incubating_tokens  # alias for older code
        self.graduated_mints: Set[str] = set()
        self.ws_healthy: bool = False
        self.tokens_seen: int = 0
        self.metadata_resolved_count: int = 0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self.run_forever(), name="pump-scanner")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()

    @staticmethod
    def _normalize_ipfs_urls(uri: str) -> List[str]:
        uri = (uri or "").strip()
        if not uri:
            return []
        if uri.startswith("ipfs://"):
            cid = uri.replace("ipfs://", "").lstrip("/")
            return [f"{gw}{cid}" for gw in IPFS_GATEWAYS]
        for gw in IPFS_GATEWAYS:
            if gw in uri:
                cid = uri.split(gw, 1)[1]
                return [uri] + [f"{other}{cid}" for other in IPFS_GATEWAYS if other != gw]
        if uri.startswith("http://") or uri.startswith("https://"):
            return [uri]
        return []

    async def resolve_token_metadata(
        self,
        session: Any,
        mint: str,
        raw_uri: str,
        fallback_payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        for url in self._normalize_ipfs_urls(raw_uri):
            try:
                data = await self._http_json(session, url, timeout=4.5)
                if isinstance(data, dict) and (data.get("name") or data.get("symbol")):
                    self.metadata_resolved_count += 1
                    return data
            except Exception:
                continue

        try:
            dex_json = await self._http_json(
                session, f"{DEXSCREENER}/{mint}", timeout=4.5
            )
            pairs = (dex_json or {}).get("pairs") or []
            if pairs:
                p0 = pairs[0]
                base = p0.get("baseToken") or {}
                info = p0.get("info") or {}
                socials = info.get("socials") or []
                websites = info.get("websites") or []
                meta = {
                    "name": base.get("name") or fallback_payload.get("name"),
                    "symbol": base.get("symbol") or fallback_payload.get("symbol"),
                    "description": info.get("header") or "",
                    "twitter": next(
                        (s.get("url") for s in socials if s.get("type") == "twitter"), ""
                    ),
                    "telegram": next(
                        (s.get("url") for s in socials if s.get("type") == "telegram"), ""
                    ),
                    "website": websites[0].get("url") if websites else "",
                    "price": float(p0["priceUsd"]) if p0.get("priceUsd") else None,
                }
                if meta["name"] or meta["symbol"]:
                    self.metadata_resolved_count += 1
                    return meta
        except Exception:
            pass

        if fallback_payload.get("name") and fallback_payload.get("symbol") and raw_uri:
            self.metadata_resolved_count += 1
            return {
                "name": fallback_payload.get("name"),
                "symbol": fallback_payload.get("symbol"),
                "description": fallback_payload.get("description", ""),
                "twitter": fallback_payload.get("twitter", ""),
                "telegram": fallback_payload.get("telegram", ""),
                "website": fallback_payload.get("website", ""),
                "uri": raw_uri,
            }
        return None

    async def _http_json(self, session: Any, url: str, timeout: float = 5.0) -> Any:
        if hasattr(session, "get") and "aiohttp" in type(session).__module__:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
        r = await session.get(url, timeout=timeout)
        if getattr(r, "status_code", 200) >= 400:
            return None
        return r.json()

    async def fetch_top5_holders_pct(
        self,
        session: Any,
        mint: str,
        bonding_curve_key: str,
        creator_wallet: str,
    ) -> Optional[Tuple[float, float]]:
        """Live RPC holder concentration. Returns None on failure (fail-closed)."""
        _ = creator_wallet
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenLargestAccounts",
            "params": [mint],
        }
        try:
            if hasattr(session, "post") and "aiohttp" in type(session).__module__:
                async with session.post(
                    SOLANA_PUBLIC_RPC, json=payload, timeout=5.0
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            else:
                r = await session.post(SOLANA_PUBLIC_RPC, json=payload, timeout=5.0)
                if r.status_code >= 400:
                    return None
                data = r.json()
            if data.get("error"):
                return None
            accounts = ((data.get("result") or {}).get("value")) or []
            if not accounts:
                return None
            total_supply = 1_000_000_000.0
            non_curve_amounts: List[float] = []
            for idx, acc in enumerate(accounts):
                amt = float(acc.get("uiAmount") or 0.0)
                addr = str(acc.get("address") or "")
                if idx == 0 and amt > 400_000_000:
                    continue
                if bonding_curve_key and addr == bonding_curve_key:
                    continue
                non_curve_amounts.append(amt)
            if not non_curve_amounts:
                return None
            top5_pct = sum(non_curve_amounts[:5]) / total_supply
            creator_pct = non_curve_amounts[0] / total_supply
            return round(min(1.0, creator_pct), 4), round(min(1.0, top5_pct), 4)
        except Exception as exc:
            logger.debug("holder RPC fail-closed for %s: %s", mint[:8], exc)
            return None

    async def evaluate_matured_tokens(self, session: Any) -> None:
        now = time.time()
        for mint in list(self.incubating_tokens.keys()):
            st = self.incubating_tokens[mint]
            age_seconds = int(now - st.created_at)
            if age_seconds > 600:
                del self.incubating_tokens[mint]
                continue
            if age_seconds < 120 or st.evaluated:
                continue
            st.evaluated = True

            creator = st.creator
            if self.tracker and hasattr(self.tracker, "is_deployer_blacklisted"):
                if self.tracker.is_deployer_blacklisted(creator):
                    logger.info(
                        "Stage 1.5 BLOCK: Blacklisted deployer %s on %s",
                        creator[:8],
                        mint[:8],
                    )
                    del self.incubating_tokens[mint]
                    continue

            metadata = await self.resolve_token_metadata(
                session, mint, st.uri, st.raw_payload or {"name": st.name, "symbol": st.symbol}
            )
            if not metadata:
                # Keep waiting a bit longer before drop
                if age_seconds > 400:
                    del self.incubating_tokens[mint]
                else:
                    st.evaluated = False
                continue

            buyers_count = len(st.buyers)
            bonding_curve_pct = float(st.bonding_curve_pct)
            if buyers_count < 5 or bonding_curve_pct >= 0.40:
                del self.incubating_tokens[mint]
                continue

            dex_data = None
            try:
                dex_data = await self._http_json(session, f"{DEXSCREENER}/{mint}", timeout=4.5)
            except Exception:
                pass
            holders = await self.fetch_top5_holders_pct(
                session, mint, st.bonding_curve_key, creator
            )
            if holders is None:
                logger.info(
                    "Stage 2 BLOCK: no live holder RPC for %s (fail-closed)", mint[:8]
                )
                del self.incubating_tokens[mint]
                continue
            creator_pct, top5_pct = holders

            wash_trade_flag = st.ping_pong_wallets >= 3
            risk_score = (
                (creator_pct * 15.0)
                + (top5_pct * 5.5)
                + min(2.5, st.sniper_count * 0.4)
                + (2.5 if wash_trade_flag else 0.0)
            )
            risk_score = round(min(10.0, max(1.0, risk_score)), 2)
            if risk_score > 7.0 or creator_pct >= 0.25 or top5_pct >= 0.80:
                del self.incubating_tokens[mint]
                continue

            cutoff_60s = now - 60.0
            buys_60s = [t for t in st.trades if t["ts"] >= cutoff_60s and t["is_buy"]]
            sells_60s = [t for t in st.trades if t["ts"] >= cutoff_60s and not t["is_buy"]]
            buy_vol_sol = sum(t["sol"] for t in buys_60s) + 0.5
            sell_vol_sol = sum(t["sol"] for t in sells_60s) + 0.5
            buy_sell_ratio = round(buy_vol_sol / sell_vol_sol, 2)

            sm_hits, sm_score, _, sm_aliases = 0, 0.0, "NONE", []
            if self.tracker and hasattr(self.tracker, "get_asset_smart_money_summary"):
                sm_hits, sm_score, _, sm_aliases = self.tracker.get_asset_smart_money_summary(
                    mint
                )
            elif self.tracker and hasattr(self.tracker, "hits_for_asset"):
                hits = self.tracker.hits_for_asset(mint, within_seconds=300)
                sm_hits = len(hits)
                sm_aliases = [h.wallet for h in hits]

            if self.sentiment_scorer and hasattr(self.sentiment_scorer, "score_solana_candidate"):
                sentiment = await self.sentiment_scorer.score_solana_candidate(
                    session=session,
                    mint=mint,
                    ticker=str(metadata.get("symbol") or st.symbol),
                    name=str(metadata.get("name") or st.name),
                    description=str(metadata.get("description") or ""),
                    metadata=metadata,
                    age_seconds=age_seconds,
                    buyers=buyers_count,
                    bonding_curve_pct=bonding_curve_pct,
                    trades_last_60s=len(buys_60s) + len(sells_60s),
                    buy_sell_ratio_60s=buy_sell_ratio,
                    smart_money_hits=sm_hits,
                    dex_data=dex_data if isinstance(dex_data, dict) else None,
                )
            else:
                sr = score_velocity(
                    mention_acceleration=0.5 if sm_hits else 0.25,
                    social_links_present=has_social_metadata(metadata),
                    text_blob=extract_text_signals(metadata),
                    smart_money_convergence=sm_hits,
                    catalyst_started_at=st.created_at,
                )

                class _S:
                    x_velocity_score = sr.x_velocity_score
                    timing_score = sr.macro_timing_score
                    exhaustion_risk = sr.exhaustion_risk
                    likelihood_pos = 0.7 if sm_hits else 0.55
                    likelihood_neg = 0.4
                    narrative_hint = extract_text_signals(metadata)[:120]

                sentiment = _S()

            if sentiment.exhaustion_risk == "HIGH" and sm_hits == 0:
                del self.incubating_tokens[mint]
                continue

            last_price = float(
                metadata.get("price") or st.last_price or 0.000035
            )
            bid = round(last_price * 0.996, 9)
            ask = round(last_price * 1.004, 9)
            q_bid = round(buy_vol_sol * 100.0, 1)
            q_ask = round(sell_vol_sol * 100.0, 1)
            microprice = calc_microprice(bid, ask, q_bid, q_ask)
            imbalance = calc_imbalance(q_bid, q_ask)

            prior_p = 0.53 + (0.04 if sm_hits > 0 else 0.0)
            posterior_p = bayesian_posterior(
                prior_p, sentiment.likelihood_pos, sentiment.likelihood_neg
            )

            if compute_scalp_targets is not None:
                targets = compute_scalp_targets("pumpfun", last_price, microprice, imbalance, 0.2)
                kelly = quarter_kelly_scalp(
                    posterior_p,
                    take_profit_pct=targets.take_profit_pct,
                    stop_loss_pct=targets.stop_loss_pct,
                )
                tp, sl, hold, trail = (
                    targets.take_profit_price,
                    targets.stop_loss_price,
                    targets.time_stop_seconds,
                    targets.trailing_stop_pct,
                )
            else:
                kelly = quarter_kelly_scalp(posterior_p, payout_odds_b=2.2)
                tp = round(ask * 1.35, 9)
                sl = round(bid * 0.88, 9)
                hold = 720
                trail = 0.12

            metrics_score = round(
                min(
                    98.0,
                    max(
                        50.0,
                        (10.0 - risk_score) * 7.5
                        + (imbalance * 18.0)
                        + (10.0 if sm_hits > 0 else 0.0)
                        + min(12.0, buyers_count * 0.25),
                    ),
                ),
                2,
            )

            packet = {
                "packet_id": f"pkt_sol_{uuid.uuid4().hex[:8]}",
                "desk": "pumpfun",
                "asset_id": mint,
                "mint": mint,
                "ticker": str(metadata.get("symbol") or st.symbol),
                "ticker_or_slug": str(metadata.get("symbol") or st.symbol or mint[:8]),
                "age_seconds": age_seconds,
                "buyers": buyers_count,
                "bonding_curve_pct": round(bonding_curve_pct, 4),
                "has_metadata": True,
                "creator": creator,
                "creator_wallet": creator,
                "creator_holding_pct": creator_pct,
                "top5_holders_pct": top5_pct,
                "sniper_count": st.sniper_count,
                "wash_trade_flag": wash_trade_flag,
                "stage2_risk_score": risk_score,
                "stage2_metrics_score": metrics_score,
                "metrics_score": metrics_score,
                "smart_money_signal": sm_hits > 0,
                "smart_money_hits": sm_hits,
                "smart_money_max_score": sm_score,
                "smart_money_wallets": sm_aliases,
                "trigger": "smart_money_entry" if sm_hits else "x_velocity_spike",
                "x_velocity_score": sentiment.x_velocity_score,
                "timing_score": sentiment.timing_score,
                "macro_timing_score": sentiment.timing_score,
                "exhaustion_risk": sentiment.exhaustion_risk,
                "narrative_hint": sentiment.narrative_hint,
                "bid": bid,
                "ask": ask,
                "q_bid": q_bid,
                "q_ask": q_ask,
                "microprice": microprice,
                "imbalance": imbalance,
                "prior_win_prob": round(prior_p, 3),
                "likelihood_pos": sentiment.likelihood_pos,
                "likelihood_neg": sentiment.likelihood_neg,
                "posterior_prob": posterior_p,
                "bayesian_posterior": posterior_p,
                "quarter_kelly_pct": kelly,
                "kelly_pct": kelly,
                "side": "BUY",
                "entry_price": last_price,
                "take_profit_price": tp,
                "stop_loss_price": sl,
                "trailing_stop_pct": trail,
                "max_hold_seconds": hold,
                "partial_take_profit_pct": 0.25,
                "curve_liquidity_usd": round(float(st.sol_in_curve) * 185.0, 2),
                "stage1": {
                    "buyers": buyers_count,
                    "bonding_curve_pct": bonding_curve_pct,
                    "age_seconds": age_seconds,
                    "metadata_resolved": True,
                },
                "stage2": {
                    "risk_score": risk_score,
                    "creator_holding_pct": creator_pct,
                    "top5_holders_pct": top5_pct,
                },
                "status": "pending",
                "created_at": utcnow(),
            }
            self.emitter.emit_packet(packet)
            del self.incubating_tokens[mint]

    async def run_forever(self, session: Any = None) -> None:
        try:
            import websockets
        except ImportError:
            logger.warning("websockets missing; pump scanner idle")
            return

        owns_session = False
        if session is None:
            if self.http is not None:
                session = self.http
            elif httpx is not None:
                session = httpx.AsyncClient(timeout=25.0)
                owns_session = True
            else:
                logger.warning("no HTTP session for PumpFunScanner")
                return

        backoff = 1.0
        try:
            while not self._stop.is_set():
                try:
                    async with websockets.connect(
                        PUMPPORTAL_WS_URL,
                        ping_interval=20,
                        ping_timeout=20,
                        close_timeout=5,
                        max_queue=2048,
                        open_timeout=20,
                    ) as ws:
                        self.ws_healthy = True
                        backoff = 1.0
                        logger.info("Connected to PumpPortal WebSocket (%s)", PUMPPORTAL_WS_URL)
                        await ws.send(json.dumps({"method": "subscribeNewToken"}))
                        await ws.send(json.dumps({"method": "subscribeMigration"}))
                        tracked_keys: list[str] = []
                        if self.tracker and hasattr(self.tracker, "solana_tracked_wallets"):
                            tracked_keys = list(self.tracker.solana_tracked_wallets.keys())
                        if tracked_keys:
                            await ws.send(
                                json.dumps(
                                    {"method": "subscribeAccountTrade", "keys": tracked_keys}
                                )
                            )

                        last_eval_ts = time.time()
                        while not self._stop.is_set():
                            try:
                                raw_msg = await asyncio.wait_for(ws.recv(), timeout=10)
                            except asyncio.TimeoutError:
                                raw_msg = None

                            now = time.time()
                            if (now - last_eval_ts) >= 5.0:
                                last_eval_ts = now
                                asyncio.create_task(self.evaluate_matured_tokens(session))

                            if raw_msg is None:
                                continue
                            try:
                                msg = json.loads(raw_msg)
                            except Exception:
                                continue
                            if not isinstance(msg, dict):
                                continue

                            if self.tracker and hasattr(
                                self.tracker, "ingest_solana_wallet_trade"
                            ):
                                self.tracker.ingest_solana_wallet_trade(msg)

                            mint = str(msg.get("mint") or "").strip()
                            if not mint:
                                continue
                            tx_type = str(msg.get("txType") or "").lower()

                            if tx_type == "create" or (
                                "uri" in msg and mint not in self.incubating_tokens
                            ):
                                self.tokens_seen += 1
                                v_sol = float(msg.get("vSolInBondingCurve") or 30.0)
                                if v_sol > 1e8:
                                    v_sol /= 1e9
                                curve_pct = min(1.0, max(0.0, (v_sol - 30.0) / 85.0))
                                creator = str(
                                    msg.get("traderPublicKey") or msg.get("creator") or ""
                                )
                                self.incubating_tokens[mint] = PendingPumpToken(
                                    mint=mint,
                                    name=str(msg.get("name") or "Unknown"),
                                    symbol=str(msg.get("symbol") or "UNKNOWN"),
                                    uri=str(msg.get("uri") or ""),
                                    creator=creator,
                                    bonding_curve_key=str(msg.get("bondingCurveKey") or ""),
                                    created_at=now,
                                    buyers={creator} if creator else set(),
                                    bonding_curve_pct=curve_pct,
                                    sol_in_curve=v_sol,
                                    raw_payload=msg,
                                )
                                try:
                                    await ws.send(
                                        json.dumps(
                                            {"method": "subscribeTokenTrade", "keys": [mint]}
                                        )
                                    )
                                except Exception:
                                    pass

                            elif mint in self.incubating_tokens:
                                st = self.incubating_tokens[mint]
                                trader = str(msg.get("traderPublicKey") or "")
                                is_buy = tx_type == "buy"
                                sol_amt = float(msg.get("solAmount") or 0.2)
                                if sol_amt > 1e8:
                                    sol_amt /= 1e9
                                if is_buy and trader:
                                    st.buyers.add(trader)
                                    if (now - st.created_at) <= 4.0 and trader != st.creator:
                                        st.sniper_count += 1
                                if trader:
                                    cnt = st.wallet_trade_counts.get(trader, 0) + 1
                                    st.wallet_trade_counts[trader] = cnt
                                    if cnt == 4:
                                        st.ping_pong_wallets += 1
                                v_sol = float(
                                    msg.get("vSolInBondingCurve") or st.sol_in_curve
                                )
                                if v_sol > 1e8:
                                    v_sol /= 1e9
                                st.sol_in_curve = v_sol
                                st.bonding_curve_pct = min(
                                    1.0, max(0.0, (v_sol - 30.0) / 85.0)
                                )
                                st.trades.append(
                                    {"ts": now, "is_buy": is_buy, "sol": sol_amt}
                                )

                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.ws_healthy = False
                    logger.warning(
                        "PumpPortal WS disconnected (%s). Reconnecting in %.1fs...",
                        exc,
                        backoff,
                    )
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(10.0, backoff * 2.0)
        finally:
            if owns_session and session is not None:
                await session.aclose()


# Back-compat alias
PumpfunScanner = PumpFunScanner


async def run_all_scanners() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    import aiohttp

    copy_tracker = CopyTracker() if CopyTracker else SmartMoneyCopyTracker()
    if hasattr(copy_tracker, "__aenter__"):
        await copy_tracker.__aenter__()
    sentiment_scorer = XSentimentScorer() if XSentimentScorer else None
    emitter = PacketEmitter(max_packets_per_5m=2, cooldown_seconds=7200)
    pump_scanner = PumpFunScanner(emitter, copy_tracker, sentiment_scorer=sentiment_scorer)
    poly_scanner = PolymarketScanner(
        emitter, copy_tracker, sentiment_scorer=sentiment_scorer
    )

    async with aiohttp.ClientSession(
        headers={"User-Agent": "WarRoomQuantEngine/2.0"}
    ) as session:
        if hasattr(copy_tracker, "refresh_polymarket_leaderboard"):
            await copy_tracker.refresh_polymarket_leaderboard(session)

        async def poly_loop() -> None:
            last_lb_refresh = time.time()
            while True:
                try:
                    if (time.time() - last_lb_refresh) >= 1800.0:
                        await copy_tracker.refresh_polymarket_leaderboard(session)
                        last_lb_refresh = time.time()
                    if hasattr(copy_tracker, "poll_polymarket_whale_activity"):
                        await copy_tracker.poll_polymarket_whale_activity(session)
                    await poly_scanner.scan_once(session)
                except Exception as exc:
                    logger.warning("Polymarket scan cycle warning: %s", exc)
                await asyncio.sleep(60.0)

        await asyncio.gather(pump_scanner.run_forever(session), poly_loop())


if __name__ == "__main__":
    asyncio.run(run_all_scanners())
