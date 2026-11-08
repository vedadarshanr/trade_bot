#!/usr/bin/env python3
"""
Polymarket Leaderboard (/v2/leaderboard + /activity) & Solana Pump.fun Smart-Money
Copy-Trading Tracker + SMART_WALLETS.md Auto-Generator.

Uses war_room_engine.db when present; falls back to local SQLite schemas otherwise.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore

logger = logging.getLogger("war_room.copy_tracker")

try:
    from war_room_engine.db import Database, get_db, utcnow
    from war_room_engine.paths import DECISIONS_MD, DB_PATH, ROOT, SMART_WALLETS_MD, STATE
except ImportError:  # pragma: no cover
    ROOT = Path(os.path.expanduser("~/workspace/trading-war-room"))
    STATE = ROOT / "state"
    DB_PATH = STATE / "war_room.db"
    SMART_WALLETS_MD = ROOT / "SMART_WALLETS.md"
    DECISIONS_MD = ROOT / "DECISIONS.md"
    Database = None  # type: ignore

    def get_db():  # type: ignore
        raise RuntimeError("db module unavailable")

    def utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()

POLY_LEADERBOARD_V2 = "https://data-api.polymarket.com/v2/leaderboard"
POLY_LEADERBOARD_V1 = "https://data-api.polymarket.com/leaderboard"
POLY_ACTIVITY_V2 = "https://data-api.polymarket.com/v2/activity"
POLY_ACTIVITY_V1 = "https://data-api.polymarket.com/activity"
PUMP_WS = "wss://pumpportal.fun/api/data"

# Curated seed alpha wallets on Solana Pump.fun
SEED_SOLANA_ALPHA_WALLETS: List[Dict[str, Any]] = [
    {
        "wallet_address": "CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o",
        "alias": "SolAlpha_CyaE",
        "pnl_usd": 142500.0,
        "win_rate": 0.64,
        "copy_score": 89.5,
    },
    {
        "wallet_address": "DNfuF1L62WWyW3pNakVkyGGFzVVhj4Yr52jSmdTyeBHm",
        "alias": "CurveSniper_DNfu",
        "pnl_usd": 98400.0,
        "win_rate": 0.61,
        "copy_score": 86.0,
    },
    {
        "wallet_address": "suqh5sHtr8HyJ7q8scBimULPkPpA557prMG47xCHQfK",
        "alias": "GradWhale_suqh",
        "pnl_usd": 84100.0,
        "win_rate": 0.59,
        "copy_score": 83.2,
    },
    {
        "wallet_address": "8zFZHuSRuDpuAR7J6FzwyF3vKNx4CVW3DFHJerQhc7Zd",
        "alias": "MomentumScalp_8zFZ",
        "pnl_usd": 63900.0,
        "win_rate": 0.58,
        "copy_score": 80.5,
    },
]

SEED_POLYMARKET_WHALES: List[Dict[str, Any]] = [
    {
        "wallet_address": "0x56687bf447db6ffa42ffe2204a05edaa20f55839",
        "alias": "Theo4_PolyWhale",
        "pnl_usd": 485000.0,
        "win_rate": 0.68,
        "copy_score": 94.0,
    },
    {
        "wallet_address": "0x1f2dd6d473f3e824cd2f8a89d9c69fb96f6ad0cf",
        "alias": "Fredi9999_CLOB",
        "pnl_usd": 312000.0,
        "win_rate": 0.64,
        "copy_score": 90.5,
    },
    {
        "wallet_address": "0x7fb7ad0d194d7123e711e7db6c9d418fac14e33d",
        "alias": "Domah_MacroSharp",
        "pnl_usd": 194000.0,
        "win_rate": 0.62,
        "copy_score": 87.0,
    },
]

# Back-compat alias
SOLANA_SEED_WALLETS = SEED_SOLANA_ALPHA_WALLETS


@dataclass
class SmartMoneyHit:
    desk: str
    wallet: str
    asset_id: str
    side: str
    ts: float
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SmartMoneySignal:
    desk: str
    asset_id: str
    wallet_address: str
    wallet_alias: str
    copy_score: float
    side: str
    usd_notional: float
    price: float
    timestamp: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SmartMoneyCopyTracker:
    """
    SQLite-backed smart wallet + creator_memory tracker with Polymarket leaderboard
    polling and Solana Pump.fun trade ingestion. Prefer CopyTracker for daemon use.
    """

    def __init__(self, db_path: Path | None = None, db: Any = None) -> None:
        self.db = db
        self.db_path = Path(db_path) if db_path else (Path(self.db.path) if db is not None else DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.db is None:
            self._ensure_tables()

        self.recent_signals: Dict[str, List[SmartMoneySignal]] = {}
        self.solana_tracked_wallets: Dict[str, Dict[str, Any]] = {}
        self.polymarket_tracked_wallets: Dict[str, Dict[str, Any]] = {}
        self.blacklisted_deployers: Set[str] = set()

        self._seed_initial_wallets()
        self.reload_blacklists()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_tables(self) -> None:
        conn = self._get_conn()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS smart_wallets (
                    wallet_address TEXT PRIMARY KEY,
                    desk TEXT,
                    alias TEXT,
                    pnl_usd REAL DEFAULT 0.0,
                    win_rate REAL DEFAULT 0.5,
                    copy_score REAL DEFAULT 50.0,
                    last_active TEXT,
                    is_whitelisted INTEGER DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS creator_memory (
                    deployer_wallet TEXT PRIMARY KEY,
                    last_token_mint TEXT,
                    outcome TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _upsert_wallet_row(self, **fields: Any) -> None:
        if self.db is not None:
            self.db.upsert_wallet(**fields)
            return
        conn = self._get_conn()
        try:
            conn.execute(
                """
                INSERT INTO smart_wallets
                (wallet_address, desk, alias, pnl_usd, win_rate, copy_score, last_active, is_whitelisted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet_address) DO UPDATE SET
                    desk=excluded.desk,
                    alias=excluded.alias,
                    pnl_usd=excluded.pnl_usd,
                    win_rate=excluded.win_rate,
                    copy_score=excluded.copy_score,
                    last_active=excluded.last_active,
                    is_whitelisted=excluded.is_whitelisted
                """,
                (
                    fields["wallet_address"],
                    fields.get("desk", "polymarket"),
                    fields.get("alias"),
                    float(fields.get("pnl_usd") or 0),
                    float(fields.get("win_rate") or 0),
                    float(fields.get("copy_score") or 0),
                    fields.get("last_active") or utcnow(),
                    1 if fields.get("is_whitelisted", True) else 0,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _seed_initial_wallets(self) -> None:
        now_iso = utcnow()
        for w in SEED_SOLANA_ALPHA_WALLETS:
            self._upsert_wallet_row(
                wallet_address=w["wallet_address"],
                desk="pumpfun",
                alias=w["alias"],
                pnl_usd=w["pnl_usd"],
                win_rate=w["win_rate"],
                copy_score=w["copy_score"],
                last_active=now_iso,
                is_whitelisted=True,
            )
        for w in SEED_POLYMARKET_WHALES:
            self._upsert_wallet_row(
                wallet_address=w["wallet_address"].lower(),
                desk="polymarket",
                alias=w["alias"],
                pnl_usd=w["pnl_usd"],
                win_rate=w["win_rate"],
                copy_score=w["copy_score"],
                last_active=now_iso,
                is_whitelisted=True,
            )
        self._load_wallets_from_db()

    def _load_wallets_from_db(self) -> None:
        self.solana_tracked_wallets.clear()
        self.polymarket_tracked_wallets.clear()
        if self.db is not None:
            for r in self.db.list_wallets(limit=200):
                d = dict(r)
                desk = d.get("desk")
                if desk in ("pumpfun", "SOLANA_PUMPFUN"):
                    self.solana_tracked_wallets[d["wallet_address"]] = d
                elif desk in ("polymarket", "POLYMARKET_CLOB"):
                    self.polymarket_tracked_wallets[d["wallet_address"].lower()] = d
            return
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM smart_wallets WHERE is_whitelisted = 1 ORDER BY copy_score DESC"
            ).fetchall()
            for r in rows:
                d = dict(r)
                if d["desk"] in ("pumpfun", "SOLANA_PUMPFUN"):
                    self.solana_tracked_wallets[d["wallet_address"]] = d
                elif d["desk"] in ("polymarket", "POLYMARKET_CLOB"):
                    self.polymarket_tracked_wallets[d["wallet_address"].lower()] = d
        finally:
            conn.close()

    def reload_blacklists(self) -> None:
        self.blacklisted_deployers.clear()
        if self.db is not None:
            try:
                with self.db.cursor() as cur:
                    cur.execute(
                        "SELECT deployer_wallet FROM creator_memory "
                        "WHERE upper(outcome) IN ('RUG','DUMP','COLLAPSE','BLACKLIST')"
                    )
                    for r in cur.fetchall():
                        self.blacklisted_deployers.add(str(r["deployer_wallet"]).strip())
            except Exception as exc:
                logger.debug("creator_memory read warning: %s", exc)
        else:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT deployer_wallet FROM creator_memory "
                    "WHERE outcome IN ('RUG','DUMP','COLLAPSE','BLACKLIST')"
                ).fetchall()
                for r in rows:
                    self.blacklisted_deployers.add(str(r["deployer_wallet"]).strip())
            except Exception as exc:
                logger.debug("creator_memory read warning: %s", exc)
            finally:
                conn.close()

        if DECISIONS_MD.exists():
            try:
                content = DECISIONS_MD.read_text(encoding="utf-8")
                for line in content.splitlines():
                    if "BLACKLISTED" in line and "[" in line and "]" in line:
                        arr_str = line[line.find("[") : line.rfind("]") + 1]
                        for item in json.loads(arr_str):
                            self.blacklisted_deployers.add(str(item).strip())
            except Exception:
                pass

    def is_deployer_blacklisted(self, deployer_wallet: str) -> bool:
        if not deployer_wallet:
            return False
        return deployer_wallet.strip() in self.blacklisted_deployers

    def record_deployer_outcome(self, deployer_wallet: str, mint: str, outcome: str) -> None:
        if not deployer_wallet:
            return
        outcome_u = outcome.upper()
        if self.db is not None and hasattr(self.db, "remember_creator"):
            try:
                self.db.remember_creator(deployer_wallet.strip(), mint, outcome_u)
            except Exception as exc:
                logger.debug("record_deployer_outcome db warn: %s", exc)
        elif self.db is not None:
            try:
                with self.db.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO creator_memory (deployer_wallet, last_token_mint, outcome, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(deployer_wallet) DO UPDATE SET
                            last_token_mint=excluded.last_token_mint,
                            outcome=excluded.outcome,
                            updated_at=excluded.updated_at
                        """,
                        (deployer_wallet.strip(), mint, outcome_u, utcnow()),
                    )
            except Exception as exc:
                logger.debug("record_deployer_outcome db warn: %s", exc)
        else:
            conn = self._get_conn()
            try:
                conn.execute(
                    """
                    INSERT INTO creator_memory (deployer_wallet, last_token_mint, outcome, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(deployer_wallet) DO UPDATE SET
                        last_token_mint = excluded.last_token_mint,
                        outcome = excluded.outcome,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (deployer_wallet.strip(), mint, outcome_u),
                )
                conn.commit()
            finally:
                conn.close()
        if outcome_u in ("RUG", "DUMP", "COLLAPSE", "BLACKLIST"):
            self.blacklisted_deployers.add(deployer_wallet.strip())

    def ingest_solana_wallet_trade(self, payload: Dict[str, Any]) -> Optional[SmartMoneySignal]:
        trader = str(payload.get("traderPublicKey") or payload.get("user") or "").strip()
        if not trader or trader not in self.solana_tracked_wallets:
            return None
        mint = str(payload.get("mint") or "").strip()
        tx_type = str(payload.get("txType") or payload.get("isBuy") or "").lower()
        is_buy = tx_type in ("buy", "true", "1")
        sol_amount = float(payload.get("solAmount") or 0.0)
        if sol_amount > 1e8:
            sol_amount /= 1e9
        wallet_info = self.solana_tracked_wallets[trader]
        sig = SmartMoneySignal(
            desk="pumpfun",
            asset_id=mint,
            wallet_address=trader,
            wallet_alias=str(wallet_info.get("alias") or trader[:8]),
            copy_score=float(wallet_info.get("copy_score") or 0),
            side="BUY" if is_buy else "SELL",
            usd_notional=round(sol_amount * 185.0, 2),
            price=float(payload.get("price") or 0.0),
            timestamp=time.time(),
        )
        if is_buy:
            self._record_signal(sig)
        return sig

    def _record_signal(self, sig: SmartMoneySignal) -> None:
        bucket = self.recent_signals.setdefault(sig.asset_id, [])
        if not any(existing.wallet_address == sig.wallet_address for existing in bucket):
            bucket.append(sig)

    def _prune_old_signals(self, max_age_seconds: int = 600) -> None:
        cutoff = time.time() - max_age_seconds
        for asset_id in list(self.recent_signals.keys()):
            fresh = [s for s in self.recent_signals[asset_id] if s.timestamp >= cutoff]
            if fresh:
                self.recent_signals[asset_id] = fresh
            else:
                del self.recent_signals[asset_id]

    def get_asset_smart_money_summary(
        self, asset_id: str, slug: str = ""
    ) -> Tuple[int, float, str, List[str]]:
        self._prune_old_signals(600)
        signals = list(self.recent_signals.get(asset_id, []))
        if slug and slug in self.recent_signals:
            signals.extend(self.recent_signals[slug])
        if not signals:
            return 0, 0.0, "NONE", []
        max_score = max(s.copy_score for s in signals)
        aliases = list({f"{s.wallet_alias}({s.copy_score:.0f})" for s in signals})
        dominant_side = signals[-1].side
        return len(aliases), max_score, dominant_side, aliases

    def write_smart_wallets_markdown(self, md_path: Path | None = None) -> None:
        self._load_wallets_from_db()
        path = md_path or SMART_WALLETS_MD
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        poly_rows = sorted(
            self.polymarket_tracked_wallets.values(),
            key=lambda x: float(x.get("copy_score", 0.0)),
            reverse=True,
        )[:25]
        sol_rows = sorted(
            self.solana_tracked_wallets.values(),
            key=lambda x: float(x.get("copy_score", 0.0)),
            reverse=True,
        )[:25]

        lines = [
            "# SMART_WALLETS.md — Live Smart-Money Copy-Trading Watchlist",
            f"> **Last Auto-Refreshed:** `{now_str}` by `war_room_engine/copy_tracker.py`",
            "> **Usage:** Read by `OnChain-Auditor` (Seat 2) and `Chief-Dispatcher` (Seat 1).",
            "",
            "## Table A: Top Polymarket Smart-Money Scalpers (`/v2/leaderboard` Filtered)",
            "| Rank | Alias | Wallet Address | Period PnL (USD) | Est. Win Rate | Copy Score (0-100) |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
        for idx, r in enumerate(poly_rows, start=1):
            lines.append(
                f"| {idx} | **{r.get('alias') or '—'}** | `{r['wallet_address']}` | "
                f"${float(r.get('pnl_usd') or 0):,.2f} | {float(r.get('win_rate') or 0) * 100:.1f}% | "
                f"**{float(r.get('copy_score') or 0):.1f}** |"
            )
        lines.extend(
            [
                "",
                "## Table B: Top Solana Pump.fun Alpha Wallets (`subscribeAccountTrade` Filtered)",
                "| Rank | Alias | Wallet Address | Historical PnL (USD) | Win Rate (>90s Hold) | Copy Score (0-100) |",
                "| :--- | :--- | :--- | :--- | :--- | :--- |",
            ]
        )
        for idx, r in enumerate(sol_rows, start=1):
            lines.append(
                f"| {idx} | **{r.get('alias') or '—'}** | `{r['wallet_address']}` | "
                f"${float(r.get('pnl_usd') or 0):,.2f} | {float(r.get('win_rate') or 0) * 100:.1f}% | "
                f"**{float(r.get('copy_score') or 0):.1f}** |"
            )
        lines.extend(
            [
                "",
                "## Anti-Rug Creator Memory Summary",
                f"- **Blacklisted Solana Deployer Count:** `{len(self.blacklisted_deployers)}`",
                "- **Rule:** Any new launch deployed by a wallet in `creator_memory` with a prior "
                "`RUG`, `DUMP`, or `COLLAPSE` outcome is automatically dropped at Stage 1.5.",
                "",
            ]
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")


class CopyTracker(SmartMoneyCopyTracker):
    """Daemon-facing tracker: httpx client, async leaderboard/activity, Pump WS."""

    def __init__(
        self,
        db: Any = None,
        http: Any = None,
        on_hit: Callable[[SmartMoneyHit], None] | None = None,
        db_path: Path | None = None,
    ) -> None:
        resolved_db = db
        if resolved_db is None:
            try:
                resolved_db = get_db()
            except Exception:
                resolved_db = None
        super().__init__(db_path=db_path, db=resolved_db)
        self._http = http
        self._owns_http = http is None
        self.on_hit = on_hit
        self.recent_hits: list[SmartMoneyHit] = []
        self._last_leaderboard = 0.0
        self._pump_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def __aenter__(self) -> "CopyTracker":
        if self._http is None:
            if httpx is None:
                raise RuntimeError("httpx required for CopyTracker")
            self._http = httpx.AsyncClient(timeout=20.0)
        self._seed_initial_wallets()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
        if self._owns_http and self._http is not None:
            await self._http.aclose()

    @property
    def http(self) -> Any:
        if self._http is None:
            raise RuntimeError("CopyTracker used outside async context")
        return self._http

    def write_smart_wallets_md(self, md_path: Path | None = None) -> None:
        self.write_smart_wallets_markdown(md_path)

    async def refresh_polymarket_leaderboard(self, session: Any = None) -> list[dict[str, Any]]:
        """
        Prefer week ∩ month profitable wallets; fall back to union with PnL floor.
        Accepts optional aiohttp session for rails compatibility; uses httpx by default.
        """
        _ = session
        week = await self._fetch_leaderboard("week")
        month = await self._fetch_leaderboard("month")
        if not week and not month:
            week = await self._fetch_leaderboard_v1("week")
            month = await self._fetch_leaderboard_v1("month")

        week_map = {self._wallet_id(r): r for r in week if self._wallet_id(r)}
        month_map = {self._wallet_id(r): r for r in month if self._wallet_id(r)}
        common = set(week_map) & set(month_map)

        scored: list[dict[str, Any]] = []
        for wid in common:
            w = week_map[wid]
            m = month_map[wid]
            pnl_w = self._pnl(w)
            pnl_m = self._pnl(m)
            if pnl_w <= 0 or pnl_m <= 0:
                continue
            if pnl_w < 50 and pnl_m < 200:
                continue
            vol_w = self._volume(w)
            if vol_w > 0 and pnl_w / vol_w < 0.002 and pnl_w < 500:
                continue
            win = float(
                w.get("winRate")
                or w.get("win_rate")
                or m.get("winRate")
                or m.get("win_rate")
                or 0.5
            )
            if win > 1.0:
                win /= 100.0
            copy = self._copy_score(pnl_w, pnl_m, win)
            row = {
                "wallet_address": wid.lower() if wid.startswith("0x") else wid,
                "desk": "polymarket",
                "alias": self._alias(w, wid),
                "pnl_usd": round((pnl_w + pnl_m) / 2.0, 2),
                "win_rate": round(win, 4),
                "copy_score": copy,
                "last_active": utcnow(),
                "is_whitelisted": True,
            }
            scored.append(row)

        # If intersection empty (API shape drift), accept high-PnL single-window sharks.
        if not scored:
            pool = {**week_map, **month_map}
            for wid, item in pool.items():
                pnl = self._pnl(item)
                if pnl < 2500.0:
                    continue
                win = float(item.get("winRate") or item.get("win_rate") or 0.55)
                if win > 1.0:
                    win /= 100.0
                copy = self._copy_score(pnl, pnl, win)
                scored.append(
                    {
                        "wallet_address": wid.lower() if wid.startswith("0x") else wid,
                        "desk": "polymarket",
                        "alias": self._alias(item, wid),
                        "pnl_usd": round(pnl, 2),
                        "win_rate": round(win, 4),
                        "copy_score": copy,
                        "last_active": utcnow(),
                        "is_whitelisted": True,
                    }
                )

        scored.sort(key=lambda r: r["copy_score"], reverse=True)
        top = scored[:25]
        for row in top:
            self._upsert_wallet_row(**row)
        self._load_wallets_from_db()
        self.write_smart_wallets_markdown()
        self._last_leaderboard = time.time()
        logger.info("polymarket leaderboard refreshed: %d wallets", len(top))
        return top

    async def _fetch_leaderboard(self, period: str) -> list[dict[str, Any]]:
        params = {"time_period": period, "sort_by": "PNL", "limit": 50}
        try:
            r = await self.http.get(POLY_LEADERBOARD_V2, params=params)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            logger.warning("leaderboard %s failed: %s", period, exc)
            return []
        return self._normalize_leaderboard_payload(data)

    async def _fetch_leaderboard_v1(self, period: str) -> list[dict[str, Any]]:
        params = {"timePeriod": period, "orderBy": "PNL", "limit": 40}
        try:
            r = await self.http.get(POLY_LEADERBOARD_V1, params=params)
            if r.status_code >= 400:
                return []
            return self._normalize_leaderboard_payload(r.json())
        except Exception as exc:
            logger.debug("leaderboard v1 %s skipped: %s", period, exc)
            return []

    @staticmethod
    def _normalize_leaderboard_payload(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("data", "leaderboard", "users", "results"):
                if isinstance(data.get(key), list):
                    return [x for x in data[key] if isinstance(x, dict)]
        return []

    @staticmethod
    def _wallet_id(row: dict[str, Any]) -> str:
        for k in (
            "user_id",
            "proxyWallet",
            "address",
            "user",
            "wallet",
            "owner",
            "id",
        ):
            v = row.get(k)
            if v:
                return str(v)
        return ""

    @staticmethod
    def _pnl(row: dict[str, Any]) -> float:
        for k in ("pnl", "pnlUsd", "profit", "realizedPnl", "usdcPnl"):
            if row.get(k) is not None:
                try:
                    return float(row[k])
                except (TypeError, ValueError):
                    continue
        return 0.0

    @staticmethod
    def _volume(row: dict[str, Any]) -> float:
        for k in ("volume", "vol", "volumeUsd", "usdVolume"):
            if row.get(k) is not None:
                try:
                    return float(row[k])
                except (TypeError, ValueError):
                    continue
        return 0.0

    @staticmethod
    def _alias(row: dict[str, Any], wid: str) -> str:
        for k in ("user_name", "userName", "name", "username", "pseudonym", "x_username"):
            v = row.get(k)
            if v:
                return str(v)[:64]
        return wid[:10]

    @staticmethod
    def _copy_score(pnl_w: float, pnl_m: float, win: float) -> float:
        base = min(70.0, 12.0 * (max(pnl_w, 0) ** 0.35) + 8.0 * (max(pnl_m, 0) ** 0.30))
        return round(min(100.0, base + 30.0 * max(0.0, win - 0.45)), 2)

    async def poll_polymarket_activity(
        self, lookback_seconds: float = 300.0
    ) -> list[SmartMoneyHit]:
        return await self.poll_polymarket_whale_activity(lookback_seconds=lookback_seconds)

    async def poll_polymarket_whale_activity(
        self,
        session: Any = None,
        lookback_seconds: float = 300.0,
    ) -> list[SmartMoneyHit]:
        _ = session
        self._load_wallets_from_db()
        wallets = sorted(
            self.polymarket_tracked_wallets.values(),
            key=lambda x: float(x.get("copy_score", 0.0)),
            reverse=True,
        )[:15]
        hits: list[SmartMoneyHit] = []
        cutoff = time.time() - lookback_seconds
        self._prune_old_signals(600)

        for w in wallets:
            addr = w["wallet_address"]
            items = await self._fetch_activity(addr)
            for act in items:
                if not isinstance(act, dict):
                    continue
                act_type = str(act.get("type") or "TRADE").upper()
                side = str(act.get("side") or "BUY").upper()
                if act_type not in ("TRADE", "BUY", "SELL") and side not in ("BUY", "SELL", "YES", "NO"):
                    continue
                ts = self._parse_ts(act)
                if ts < cutoff:
                    continue
                asset = str(
                    act.get("conditionId")
                    or act.get("asset")
                    or act.get("market")
                    or act.get("slug")
                    or ""
                ).strip()
                if not asset:
                    continue
                usd_size = float(act.get("usdcSize") or act.get("size") or 0.0)
                if usd_size and usd_size < 150.0 and side == "BUY":
                    # Keep smaller fills for hit tracking but skip as copy signals.
                    pass
                hit = SmartMoneyHit(
                    desk="polymarket",
                    wallet=addr,
                    asset_id=asset,
                    side=str(act.get("outcome") or side),
                    ts=ts,
                    raw=act,
                )
                hits.append(hit)
                self._emit(hit)
                if usd_size >= 150.0 and side == "BUY":
                    sig = SmartMoneySignal(
                        desk="polymarket",
                        asset_id=asset,
                        wallet_address=addr,
                        wallet_alias=str(w.get("alias") or addr[:8]),
                        copy_score=float(w.get("copy_score") or 0),
                        side=str(act.get("outcome") or "YES").upper(),
                        usd_notional=round(usd_size, 2),
                        price=float(act.get("price") or 0.5),
                        timestamp=ts,
                    )
                    self._record_signal(sig)
        self.recent_hits = [h for h in self.recent_hits + hits if h.ts >= cutoff][-200:]
        return hits

    async def _fetch_activity(self, addr: str) -> list[Any]:
        for url in (POLY_ACTIVITY_V2, POLY_ACTIVITY_V1):
            try:
                r = await self.http.get(url, params={"user": addr, "limit": 5})
                if r.status_code >= 400:
                    continue
                data = r.json()
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    for key in ("data", "activity", "trades"):
                        if isinstance(data.get(key), list):
                            return data[key]
            except Exception as exc:
                logger.debug("activity %s via %s: %s", addr[:8], url, exc)
        return []

    @staticmethod
    def _parse_ts(item: dict[str, Any]) -> float:
        for k in ("timestamp", "createdAt", "time", "ts"):
            v = item.get(k)
            if v is None:
                continue
            if isinstance(v, (int, float)):
                ts = float(v)
                if ts > 1e12:
                    ts /= 1000.0
                return ts
            if isinstance(v, str):
                try:
                    return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
        return time.time()

    def hits_for_asset(self, asset_id: str, within_seconds: float = 300.0) -> list[SmartMoneyHit]:
        cutoff = time.time() - within_seconds
        hits = [h for h in self.recent_hits if h.asset_id == asset_id and h.ts >= cutoff]
        # Also surface SmartMoneySignal buckets as hits for scanners.
        for sig in self.recent_signals.get(asset_id, []):
            if sig.timestamp >= cutoff:
                hits.append(
                    SmartMoneyHit(
                        desk=sig.desk,
                        wallet=sig.wallet_address,
                        asset_id=sig.asset_id,
                        side=sig.side,
                        ts=sig.timestamp,
                    )
                )
        return hits

    def _emit(self, hit: SmartMoneyHit) -> None:
        if self.on_hit:
            self.on_hit(hit)

    async def start_pump_ws(self) -> None:
        if self._pump_task and not self._pump_task.done():
            return
        self._pump_task = asyncio.create_task(self._pump_loop(), name="pump-copy-ws")

    async def _pump_loop(self) -> None:
        try:
            import websockets
        except ImportError:
            logger.warning("websockets not installed; pump copy tracker idle")
            return

        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    PUMP_WS, ping_interval=20, ping_timeout=20, open_timeout=20
                ) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    tracked = list(self.solana_tracked_wallets.keys())
                    if tracked:
                        await ws.send(
                            json.dumps({"method": "subscribeAccountTrade", "keys": tracked})
                        )
                    logger.info("pumpportal copy ws connected (%d wallets)", len(tracked))
                    backoff = 1.0
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=60)
                        self._handle_pump_msg(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("pump ws down (%s); retry in %.0fs", exc, backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(60.0, backoff * 2)

    def _handle_pump_msg(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        sig = self.ingest_solana_wallet_trade(payload)
        if sig is None:
            return
        hit = SmartMoneyHit(
            desk="pumpfun",
            wallet=sig.wallet_address,
            asset_id=sig.asset_id,
            side=sig.side,
            ts=sig.timestamp,
            raw=payload,
        )
        self.recent_hits.append(hit)
        self._emit(hit)
