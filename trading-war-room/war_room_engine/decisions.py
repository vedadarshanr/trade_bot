"""Parse DECISIONS.md machine-readable JSON fences."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from .paths import DECISIONS_MD

_FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass
class Decisions:
    mode: str = "PAPER"
    dry_run: bool = True
    live_trading_enabled: bool = False
    paper_bankroll_usd: float = 10_000.0
    kelly_fraction: float = 0.25
    max_position_pct: float = 0.03
    daily_loss_limit_pct: float = 0.05
    drawdown_circuit_breaker_pct: float = 0.15
    max_sector_concentration_pct: float = 0.30
    max_open_positions: int = 5
    max_daily_trades: int = 25
    min_stage6_score: float = 72.0
    max_adverse_imbalance: float = -0.50
    blacklisted_solana_deployers: list[str] = field(default_factory=list)
    vetoed_polymarket_conditions: list[str] = field(default_factory=list)

    @property
    def max_notional_usd(self) -> float:
        return self.paper_bankroll_usd * self.max_position_pct


def _merge_blocks(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    merged: dict[str, Any] = {}
    for match in _FENCE.finditer(text):
        block = json.loads(match.group(1))
        if isinstance(block, dict):
            merged.update(block)
    return merged


@lru_cache(maxsize=4)
def load_decisions(path: str | None = None) -> Decisions:
    p = Path(path) if path else DECISIONS_MD
    raw = _merge_blocks(p) if p.exists() else {}
    return Decisions(
        mode=str(raw.get("MODE", "PAPER")),
        dry_run=bool(raw.get("DRY_RUN", True)),
        live_trading_enabled=bool(raw.get("LIVE_TRADING_ENABLED", False)),
        paper_bankroll_usd=float(raw.get("PAPER_BANKROLL_USD", 10_000.0)),
        kelly_fraction=float(raw.get("KELLY_FRACTION", 0.25)),
        max_position_pct=float(raw.get("MAX_POSITION_PCT", 0.03)),
        daily_loss_limit_pct=float(raw.get("DAILY_LOSS_LIMIT_PCT", 0.05)),
        drawdown_circuit_breaker_pct=float(
            raw.get("DRAWDOWN_CIRCUIT_BREAKER_PCT", 0.15)
        ),
        max_sector_concentration_pct=float(
            raw.get("MAX_SECTOR_CONCENTRATION_PCT", 0.30)
        ),
        max_open_positions=int(raw.get("MAX_OPEN_POSITIONS", 5)),
        max_daily_trades=int(raw.get("MAX_DAILY_TRADES", 25)),
        min_stage6_score=float(raw.get("MIN_STAGE6_SCORE", 72.0)),
        max_adverse_imbalance=float(raw.get("MAX_ADVERSE_IMBALANCE", -0.50)),
        blacklisted_solana_deployers=list(
            raw.get("BLACKLISTED_SOLANA_DEPLOYERS") or []
        ),
        vetoed_polymarket_conditions=list(
            raw.get("VETOED_POLYMARKET_CONDITIONS") or []
        ),
    )


def reload_decisions(path: str | None = None) -> Decisions:
    load_decisions.cache_clear()
    return load_decisions(path)
