#!/usr/bin/env python3
"""
Deterministic X (Twitter) Sentiment Velocity, Catalyst Freshness & Exhaustion Scorer.

Reads the 15-minute macro pulse cache and evaluates per-asset social acceleration
so Grok Bot seats never waste quota polling raw social feeds in chat loops.

Self-contained: imports war_room_engine.paths when present; otherwise falls back
to ~/workspace/trading-war-room paths.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("war_room.x_sentiment")

try:
    from war_room_engine.paths import MACRO_PULSE_PATH, STATE as STATE_DIR, ROOT as WAR_ROOM_ROOT
except ImportError:  # pragma: no cover - defensive standalone fallback
    WAR_ROOM_ROOT = Path(os.path.expanduser("~/workspace/trading-war-room"))
    STATE_DIR = WAR_ROOM_ROOT / "state"
    MACRO_PULSE_PATH = STATE_DIR / "macro_pulse.json"

BREAKING_CATALYST_KEYWORDS = {
    "breaking",
    "official",
    "confirmed",
    "approved",
    "etf",
    "sec",
    "fed",
    "fomc",
    "cpi",
    "trump",
    "elon",
    "musk",
    "xai",
    "grok",
    "openai",
    "solana",
    "binance",
    "coinbase",
    "listing",
    "hack",
    "exploit",
    "indictment",
    "verdict",
    "ceasefire",
    "executive order",
    "treasury",
    "rate cut",
    "emergency",
    "just in",
    "wins",
    "elected",
    "airdrop",
    "migrated",
    "raydium",
}

SPAM_OR_RUG_KEYWORDS = {
    "airdrop claim",
    "free sol",
    "send sol",
    "double your",
    "pump group",
    "telegram vip",
    "whatsapp",
    "guaranteed 100x",
    "presale link",
}

# Legacy alias used by older scanners/tests
BREAKING_KEYWORDS = tuple(sorted(BREAKING_CATALYST_KEYWORDS))


@dataclass
class SentimentSignal:
    x_velocity_score: float  # 0.0 to 100.0
    timing_score: float  # 0.0 to 100.0 (from 15m macro_pulse.json)
    catalyst_age_seconds: int
    exhaustion_risk: str  # "LOW" | "MEDIUM" | "HIGH"
    likelihood_pos: float
    likelihood_neg: float
    narrative_hint: str
    social_links_count: int = 0
    smart_account_mentions: int = 0
    bot_spam_ratio: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SentimentResult:
    """Legacy result shape kept for score_velocity() callers."""

    x_velocity_score: float
    catalyst_age_seconds: float
    exhaustion_risk: str
    macro_timing_score: float
    details: dict[str, Any]


class XSentimentScorer:
    """
    Combines:
      1. Cached 15-minute macro regime from state/macro_pulse.json
      2. On-chain & DexScreener/Polymarket social velocity proxies
      3. Optional live xAI Grok API search if XAI_API_KEY is configured
    """

    def __init__(self, macro_pulse_path: Path = MACRO_PULSE_PATH) -> None:
        self.macro_pulse_path = Path(macro_pulse_path)
        try:
            from war_room_engine.secrets import get_secret

            self.xai_api_key = get_secret("XAI_API_KEY")
        except Exception:
            self.xai_api_key = os.getenv("XAI_API_KEY", "").strip()
        self._macro_cache: Dict[str, Any] = {}
        self._macro_loaded_at: float = 0.0
        self.live_x_enabled = bool(self.xai_api_key)

    async def _live_x_velocity_boost(
        self, session: Any, query: str
    ) -> tuple[float, str]:
        """Optional xAI Live Search. Returns (score_boost_0_20, note)."""
        if not self.xai_api_key:
            return 0.0, "live_x=off"
        url = "https://api.x.ai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.xai_api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": "grok-4-fast",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return ONLY JSON: "
                        '{"mention_accel":0-1,"breaking":true|false,"spam_ratio":0-1,'
                        '"summary":"<=20 words"}'
                    ),
                },
                {
                    "role": "user",
                    "content": f"X/Twitter velocity for catalyst: {query[:280]}",
                },
            ],
            "temperature": 0,
        }
        try:
            # Support aiohttp session or httpx-like
            if hasattr(session, "post") and "aiohttp" in type(session).__module__:
                async with session.post(
                    url, json=body, headers=headers, timeout=8
                ) as resp:
                    if resp.status >= 400:
                        return 0.0, f"live_x_http={resp.status}"
                    data = await resp.json()
            else:
                resp = await session.post(url, json=body, headers=headers, timeout=8.0)
                status = getattr(resp, "status_code", getattr(resp, "status", 0))
                if status >= 400:
                    return 0.0, f"live_x_http={status}"
                data = resp.json() if callable(getattr(resp, "json", None)) else await resp.json()
            content = (
                ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
                or "{}"
            )
            # Strip fences if model wraps JSON
            content = content.strip()
            if content.startswith("```"):
                content = content.strip("`")
                if content.startswith("json"):
                    content = content[4:].strip()
            parsed = json.loads(content) if isinstance(content, str) else content
            accel = float(parsed.get("mention_accel") or 0.0)
            breaking = bool(parsed.get("breaking"))
            spam = float(parsed.get("spam_ratio") or 0.0)
            boost = min(20.0, max(0.0, accel * 14.0 + (6.0 if breaking else 0.0) - spam * 10.0))
            summary = str(parsed.get("summary") or "live-x")[:80]
            return boost, f"live_x={summary}"
        except Exception as exc:
            logger.debug("live X enrichment skipped: %s", exc)
            return 0.0, "live_x=error"

    def get_macro_pulse(self) -> Dict[str, Any]:
        """Read macro_pulse.json with a 60s in-memory buffer; self-heal if missing."""
        now = time.time()
        if self._macro_cache and (now - self._macro_loaded_at) < 60.0:
            return self._macro_cache

        default_pulse = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ttl_minutes": 15,
            "timing_score": 75.0,
            "macro_timing_score": 75.0,
            "regime": "RISK_ON_SELECTIVE",
            "solana_meme_regime": "RISK_ON_SELECTIVE",
            "polymarket_liquidity_regime": "NORMAL",
            "notes": "Auto-baseline macro pulse cache.",
        }

        try:
            if not self.macro_pulse_path.exists():
                self.macro_pulse_path.parent.mkdir(parents=True, exist_ok=True)
                self.macro_pulse_path.write_text(json.dumps(default_pulse, indent=2))
                self._macro_cache = default_pulse
                self._macro_loaded_at = now
                return default_pulse

            data = json.loads(self.macro_pulse_path.read_text(encoding="utf-8"))
            timing = float(
                data.get("timing_score")
                or data.get("macro_timing_score")
                or 75.0
            )
            timing = max(0.0, min(100.0, timing))
            data["timing_score"] = timing
            data["macro_timing_score"] = timing
            self._macro_cache = data
            self._macro_loaded_at = now
            return data
        except Exception as exc:
            logger.warning("Failed reading macro_pulse.json, using baseline: %s", exc)
            return default_pulse

    async def score_solana_candidate(
        self,
        session: Any,
        mint: str,
        ticker: str,
        name: str,
        description: str,
        metadata: Dict[str, Any],
        age_seconds: int,
        buyers: int,
        bonding_curve_pct: float,
        trades_last_60s: int,
        buy_sell_ratio_60s: float,
        smart_money_hits: int,
        dex_data: Optional[Dict[str, Any]] = None,
    ) -> SentimentSignal:
        """X narrative velocity + catalyst freshness + exhaustion for a Pump.fun mint."""
        _ = (session, mint, buyers)  # reserved for optional live xAI enrichment
        macro = self.get_macro_pulse()
        timing_score = float(macro.get("timing_score", 75.0))

        twitter_url = str(metadata.get("twitter") or "").strip()
        telegram_url = str(metadata.get("telegram") or "").strip()
        website_url = str(metadata.get("website") or "").strip()
        social_links_count = sum(1 for link in (twitter_url, telegram_url, website_url) if link)
        has_valid_x_link = bool(
            re.search(r"(x\.com|twitter\.com)/[A-Za-z0-9_]{2,}", twitter_url, re.IGNORECASE)
        )

        combined_text = f"{ticker} {name} {description}".lower()
        spam_hits = sum(1 for kw in SPAM_OR_RUG_KEYWORDS if kw in combined_text)
        catalyst_hits = sum(1 for kw in BREAKING_CATALYST_KEYWORDS if kw in combined_text)

        dex_txns_m5_buys = 0
        dex_txns_m5_sells = 0
        dex_vol_m5 = 0.0
        dex_price_change_m5 = 0.0
        dex_boosts = 0
        if dex_data and isinstance(dex_data.get("pairs"), list) and dex_data["pairs"]:
            primary_pair = dex_data["pairs"][0]
            txns_m5 = (primary_pair.get("txns") or {}).get("m5") or {}
            dex_txns_m5_buys = int(txns_m5.get("buys", 0) or 0)
            dex_txns_m5_sells = int(txns_m5.get("sells", 0) or 0)
            dex_vol_m5 = float((primary_pair.get("volume") or {}).get("m5", 0.0) or 0.0)
            dex_price_change_m5 = float(
                (primary_pair.get("priceChange") or {}).get("m5", 0.0) or 0.0
            )
            dex_boosts = int((primary_pair.get("boosts") or {}).get("active", 0) or 0)

        score = 35.0
        if has_valid_x_link:
            score += 14.0
        if social_links_count >= 2:
            score += 6.0
        if social_links_count == 3:
            score += 4.0

        effective_60s_trades = max(
            trades_last_60s, (dex_txns_m5_buys + dex_txns_m5_sells) // 3
        )
        score += min(22.0, math.log1p(max(0, effective_60s_trades)) * 6.2)

        if buy_sell_ratio_60s >= 1.8:
            score += 10.0
        elif buy_sell_ratio_60s >= 1.3:
            score += 5.0
        elif buy_sell_ratio_60s < 0.85:
            score -= 12.0

        if smart_money_hits > 0:
            score += min(18.0, smart_money_hits * 9.0)
        if dex_boosts > 0:
            score += min(8.0, dex_boosts * 2.0)
        if catalyst_hits > 0:
            score += min(8.0, catalyst_hits * 4.0)
        if spam_hits > 0:
            score -= 25.0 * spam_hits

        exhaustion_risk = "LOW"
        if bonding_curve_pct >= 0.36 and buy_sell_ratio_60s < 1.15:
            exhaustion_risk = "HIGH"
            score -= 18.0
        elif dex_price_change_m5 >= 180.0 and buy_sell_ratio_60s < 1.25:
            exhaustion_risk = "HIGH"
            score -= 15.0
        elif bonding_curve_pct >= 0.28 or dex_price_change_m5 >= 95.0:
            exhaustion_risk = "MEDIUM"
            score -= 5.0

        x_velocity_score = round(max(0.0, min(100.0, score)), 2)
        norm_v = x_velocity_score / 100.0
        if exhaustion_risk == "HIGH":
            likelihood_pos = round(max(0.30, min(0.65, norm_v * 0.70)), 3)
            likelihood_neg = round(max(0.45, min(0.80, 1.0 - (norm_v * 0.45))), 3)
        else:
            likelihood_pos = round(max(0.40, min(0.88, 0.35 + (norm_v * 0.52))), 3)
            likelihood_neg = round(max(0.20, min(0.65, 0.68 - (norm_v * 0.42))), 3)

        hint_parts = [
            f"Ticker {ticker} ({age_seconds}s old)",
            f"X-link={'YES' if has_valid_x_link else 'NO'}",
            f"60s_trades={effective_60s_trades}",
            f"B/S_ratio={buy_sell_ratio_60s:.2f}",
            f"smart_wallets={smart_money_hits}",
            f"exhaustion={exhaustion_risk}",
        ]
        if dex_vol_m5 > 0:
            hint_parts.append(f"m5_vol=${dex_vol_m5:,.0f}")

        return SentimentSignal(
            x_velocity_score=x_velocity_score,
            timing_score=timing_score,
            catalyst_age_seconds=min(int(age_seconds), 180),
            exhaustion_risk=exhaustion_risk,
            likelihood_pos=likelihood_pos,
            likelihood_neg=likelihood_neg,
            narrative_hint=" | ".join(hint_parts),
            social_links_count=social_links_count,
            smart_account_mentions=smart_money_hits,
            bot_spam_ratio=round(
                0.45 if spam_hits > 0 else (0.10 if has_valid_x_link else 0.25), 2
            ),
        )

    async def score_polymarket_candidate(
        self,
        session: Any,
        question: str,
        slug: str,
        category: str,
        volume_24hr: float,
        liquidity: float,
        imbalance: float,
        microprice_edge: float,
        mid_price: float,
        smart_money_hits: int,
        smart_money_side: str = "NONE",
    ) -> SentimentSignal:
        """Breaking-news velocity + turnover urgency for a Polymarket scalp."""
        macro = self.get_macro_pulse()
        timing_score = float(macro.get("timing_score", 75.0))

        text_lower = f"{question} {slug} {category}".lower()
        matched_keywords = [kw for kw in BREAKING_CATALYST_KEYWORDS if kw in text_lower]

        turnover_ratio = volume_24hr / max(liquidity, 1.0)
        abs_imbalance = abs(imbalance)

        score = 45.0
        score += min(18.0, math.log1p(turnover_ratio * 4.0) * 7.5)
        score += min(15.0, abs_imbalance * 24.0)
        score += min(10.0, abs(microprice_edge) * 350.0)
        if matched_keywords:
            score += min(12.0, len(matched_keywords) * 4.5)
        if smart_money_hits > 0:
            score += min(20.0, smart_money_hits * 11.0)

        live_boost, live_note = await self._live_x_velocity_boost(
            session, f"{question} {slug}"
        )
        score += live_boost

        exhaustion_risk = "LOW"
        if mid_price >= 0.93 or mid_price <= 0.07:
            exhaustion_risk = "MEDIUM"
        if abs_imbalance < 0.10 and turnover_ratio < 0.25 and smart_money_hits == 0:
            exhaustion_risk = "MEDIUM"
            score -= 8.0

        x_velocity_score = round(max(0.0, min(100.0, score)), 2)
        norm_v = x_velocity_score / 100.0
        likelihood_pos = round(max(0.45, min(0.86, 0.42 + (norm_v * 0.42))), 3)
        likelihood_neg = round(max(0.22, min(0.60, 0.62 - (norm_v * 0.38))), 3)

        kw_str = ",".join(matched_keywords[:3]) if matched_keywords else "orderflow-driven"
        hint = (
            f"Cat={category} | Turnover={turnover_ratio:.2f}x | "
            f"Imbalance={imbalance:+.2f} | SmartWallets={smart_money_hits} ({smart_money_side}) | "
            f"Catalyst={kw_str} | {live_note}"
        )

        return SentimentSignal(
            x_velocity_score=x_velocity_score,
            timing_score=timing_score,
            catalyst_age_seconds=120 if smart_money_hits > 0 else 300,
            exhaustion_risk=exhaustion_risk,
            likelihood_pos=likelihood_pos,
            likelihood_neg=likelihood_neg,
            narrative_hint=hint,
            social_links_count=1,
            smart_account_mentions=smart_money_hits,
            bot_spam_ratio=0.05,
        )


# ---------------------------------------------------------------------------
# Legacy helpers (kept for older scanners / unit callers)
# ---------------------------------------------------------------------------


def load_macro_pulse(path: Path | None = None) -> dict[str, Any]:
    scorer = XSentimentScorer(macro_pulse_path=path or MACRO_PULSE_PATH)
    pulse = scorer.get_macro_pulse()
    return {
        "regime": pulse.get("regime") or pulse.get("solana_meme_regime") or "NEUTRAL",
        "macro_timing_score": float(
            pulse.get("macro_timing_score") or pulse.get("timing_score") or 50.0
        ),
        "timing_score": float(pulse.get("timing_score") or 50.0),
        "notes": pulse.get("notes", ""),
        "updated_at": pulse.get("timestamp") or pulse.get("updated_at") or "",
        **{k: v for k, v in pulse.items() if k not in ("notes",)},
    }


def _keyword_hits(text: str) -> int:
    low = text.lower()
    return sum(1 for k in BREAKING_CATALYST_KEYWORDS if k in low)


def score_velocity(
    *,
    mention_acceleration: float = 0.0,
    social_links_present: bool = False,
    text_blob: str = "",
    smart_money_convergence: int = 0,
    price_move_pct: float = 0.0,
    catalyst_started_at: float | None = None,
    now: float | None = None,
    macro_path: Path | None = None,
) -> SentimentResult:
    """Combine macro pulse + social/microstructure velocity into 0–100 score."""
    now_ts = time.time() if now is None else now
    started = catalyst_started_at if catalyst_started_at is not None else now_ts
    age = max(0.0, now_ts - started)

    macro = load_macro_pulse(macro_path)
    macro_timing = float(macro.get("macro_timing_score") or 50.0)

    accel = max(0.0, min(2.0, float(mention_acceleration)))
    kw = _keyword_hits(text_blob)
    spam = sum(1 for s in SPAM_OR_RUG_KEYWORDS if s in text_blob.lower())
    social_bonus = 12.0 if social_links_present else 0.0
    sm_bonus = min(25.0, 8.0 * max(0, int(smart_money_convergence)))

    raw = (
        20.0
        + 30.0 * min(1.0, accel)
        + social_bonus
        + min(15.0, 5.0 * kw)
        + sm_bonus
        + 0.15 * (macro_timing - 50.0)
        - 20.0 * spam
    )
    if age > 2700:
        raw *= 0.55
    elif age > 900:
        raw *= 0.80

    score = max(0.0, min(100.0, raw))
    move = abs(float(price_move_pct))
    if move >= 0.35 or (score >= 85 and age > 600):
        exhaustion = "HIGH"
    elif move >= 0.15 or score >= 70:
        exhaustion = "MEDIUM"
    else:
        exhaustion = "LOW"

    return SentimentResult(
        x_velocity_score=round(score, 2),
        catalyst_age_seconds=round(age, 1),
        exhaustion_risk=exhaustion,
        macro_timing_score=macro_timing,
        details={
            "mention_acceleration": accel,
            "keyword_hits": kw,
            "spam_hits": spam,
            "social_links_present": social_links_present,
            "smart_money_convergence": smart_money_convergence,
            "macro_regime": macro.get("regime"),
            "price_move_pct": price_move_pct,
        },
    )


def extract_text_signals(meta: dict[str, Any]) -> str:
    parts = [
        str(meta.get("name") or ""),
        str(meta.get("symbol") or ""),
        str(meta.get("description") or ""),
        str(meta.get("question") or ""),
        str(meta.get("title") or ""),
    ]
    return " ".join(p for p in parts if p)


_HANDLE = re.compile(r"(twitter|x\.com|t\.me|telegram|discord)", re.I)


def has_social_metadata(meta: dict[str, Any]) -> bool:
    blob = json.dumps(meta, default=str)
    return bool(_HANDLE.search(blob))
