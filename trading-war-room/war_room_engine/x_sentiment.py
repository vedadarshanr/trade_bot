"""X sentiment velocity & catalyst scorer (deterministic, no live X API required)."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import MACRO_PULSE_PATH

BREAKING_KEYWORDS = (
    "breaking",
    "just in",
    "confirmed",
    "wins",
    "elected",
    "indicted",
    "launch",
    "airdrop",
    "migrated",
    "raydium",
    "listing",
)


@dataclass(frozen=True)
class SentimentResult:
    x_velocity_score: float  # 0-100
    catalyst_age_seconds: float
    exhaustion_risk: str  # LOW | MEDIUM | HIGH
    macro_timing_score: float
    details: dict[str, Any]


def load_macro_pulse(path: Path | None = None) -> dict[str, Any]:
    p = path or MACRO_PULSE_PATH
    if not p.exists():
        return {
            "regime": "NEUTRAL",
            "macro_timing_score": 50.0,
            "notes": "missing macro_pulse.json",
            "updated_at": "",
        }
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "regime": "NEUTRAL",
            "macro_timing_score": 50.0,
            "notes": "corrupt macro_pulse.json",
            "updated_at": "",
        }


def _keyword_hits(text: str) -> int:
    low = text.lower()
    return sum(1 for k in BREAKING_KEYWORDS if k in low)


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
    """Combine macro pulse + social/microstructure velocity into 0–100 score.

    `mention_acceleration` is expected as a relative rate (0 = flat, 1 = 100% accel).
    This function never calls X/Twitter; callers pass pre-computed signals from
    packet metadata / DexScreener / pump.fun fields.
    """
    now = time.time() if now is None else now
    started = catalyst_started_at if catalyst_started_at is not None else now
    age = max(0.0, now - started)

    macro = load_macro_pulse(macro_path)
    macro_timing = float(macro.get("macro_timing_score") or 50.0)

    accel = max(0.0, min(2.0, float(mention_acceleration)))
    kw = _keyword_hits(text_blob)
    social_bonus = 12.0 if social_links_present else 0.0
    sm_bonus = min(25.0, 8.0 * max(0, int(smart_money_convergence)))

    raw = (
        20.0
        + 30.0 * min(1.0, accel)
        + social_bonus
        + min(15.0, 5.0 * kw)
        + sm_bonus
        + 0.15 * (macro_timing - 50.0)
    )
    # Age decay: fresh catalysts score higher; >45m fades.
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
