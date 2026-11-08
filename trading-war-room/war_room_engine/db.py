"""SQLite state & telemetry manager."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .paths import DB_PATH, STATE

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS candidates (
    packet_id TEXT PRIMARY KEY,
    desk TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    ticker_or_slug TEXT,
    smart_money_signal INTEGER NOT NULL DEFAULT 0,
    x_velocity_score REAL NOT NULL DEFAULT 0,
    stage2_metrics_score REAL NOT NULL DEFAULT 0,
    stage6_total_score REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    packet_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS smart_wallets (
    wallet_address TEXT PRIMARY KEY,
    desk TEXT NOT NULL,
    alias TEXT,
    pnl_usd REAL NOT NULL DEFAULT 0,
    win_rate REAL NOT NULL DEFAULT 0,
    copy_score REAL NOT NULL DEFAULT 0,
    last_active TEXT,
    is_whitelisted INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS agent_votes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id TEXT NOT NULL,
    seat_name TEXT NOT NULL,
    score REAL,
    approve INTEGER,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(packet_id, seat_name)
);

CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id TEXT NOT NULL,
    desk TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    current_price REAL NOT NULL,
    peak_price REAL NOT NULL,
    microprice REAL,
    imbalance REAL,
    kelly_pct REAL NOT NULL,
    notional_usd REAL NOT NULL,
    take_profit_price REAL NOT NULL,
    stop_loss_price REAL NOT NULL,
    max_hold_seconds INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    exit_reason TEXT,
    pnl_usd REAL,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS creator_memory (
    deployer_wallet TEXT PRIMARY KEY,
    last_token_mint TEXT,
    outcome TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_votes_packet ON agent_votes(packet_id);
CREATE INDEX IF NOT EXISTS idx_fills_status ON paper_fills(status);
CREATE INDEX IF NOT EXISTS idx_wallets_desk ON smart_wallets(desk, copy_score DESC);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
            finally:
                cur.close()

    # ----- candidates -----

    def upsert_candidate(self, packet: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO candidates (
                    packet_id, desk, asset_id, ticker_or_slug, smart_money_signal,
                    x_velocity_score, stage2_metrics_score, stage6_total_score,
                    status, packet_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(packet_id) DO UPDATE SET
                    smart_money_signal=excluded.smart_money_signal,
                    x_velocity_score=excluded.x_velocity_score,
                    stage2_metrics_score=excluded.stage2_metrics_score,
                    packet_json=excluded.packet_json
                """,
                (
                    packet["packet_id"],
                    packet.get("desk", "unknown"),
                    packet.get("asset_id", ""),
                    packet.get("ticker_or_slug"),
                    1 if packet.get("smart_money_signal") else 0,
                    float(packet.get("x_velocity_score") or 0),
                    float(packet.get("stage2_metrics_score") or 0),
                    packet.get("stage6_total_score"),
                    packet.get("status", "pending"),
                    json.dumps(packet, ensure_ascii=False, default=str),
                    packet.get("created_at") or utcnow(),
                ),
            )

    def get_candidate(self, packet_id: str) -> Optional[sqlite3.Row]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM candidates WHERE packet_id=?", (packet_id,))
            return cur.fetchone()

    def list_pending_candidates(self) -> list[sqlite3.Row]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM candidates WHERE status='pending' ORDER BY created_at ASC"
            )
            return list(cur.fetchall())

    def set_candidate_status(
        self, packet_id: str, status: str, stage6: float | None = None
    ) -> None:
        with self.cursor() as cur:
            if stage6 is None:
                cur.execute(
                    "UPDATE candidates SET status=? WHERE packet_id=?",
                    (status, packet_id),
                )
            else:
                cur.execute(
                    "UPDATE candidates SET status=?, stage6_total_score=? WHERE packet_id=?",
                    (status, stage6, packet_id),
                )

    # ----- votes -----

    def upsert_vote(
        self,
        packet_id: str,
        seat_name: str,
        score: float | None,
        approve: bool | None,
        payload: dict[str, Any],
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_votes (packet_id, seat_name, score, approve, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(packet_id, seat_name) DO UPDATE SET
                    score=excluded.score,
                    approve=excluded.approve,
                    payload_json=excluded.payload_json,
                    created_at=excluded.created_at
                """,
                (
                    packet_id,
                    seat_name,
                    score,
                    None if approve is None else (1 if approve else 0),
                    json.dumps(payload, ensure_ascii=False, default=str),
                    utcnow(),
                ),
            )

    def votes_for(self, packet_id: str) -> dict[str, dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM agent_votes WHERE packet_id=?", (packet_id,))
            out: dict[str, dict[str, Any]] = {}
            for row in cur.fetchall():
                out[row["seat_name"]] = {
                    "score": row["score"],
                    "approve": None if row["approve"] is None else bool(row["approve"]),
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
            return out

    # ----- wallets -----

    def upsert_wallet(self, **fields: Any) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO smart_wallets (
                    wallet_address, desk, alias, pnl_usd, win_rate, copy_score,
                    last_active, is_whitelisted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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

    def list_wallets(self, desk: str | None = None, limit: int = 25) -> list[sqlite3.Row]:
        with self.cursor() as cur:
            if desk:
                cur.execute(
                    """
                    SELECT * FROM smart_wallets
                    WHERE desk=? AND is_whitelisted=1
                    ORDER BY copy_score DESC LIMIT ?
                    """,
                    (desk, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM smart_wallets WHERE is_whitelisted=1
                    ORDER BY copy_score DESC LIMIT ?
                    """,
                    (limit,),
                )
            return list(cur.fetchall())

    # ----- paper fills -----

    def open_fill(self, **fields: Any) -> int:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO paper_fills (
                    packet_id, desk, asset_id, side, entry_price, current_price,
                    peak_price, microprice, imbalance, kelly_pct, notional_usd,
                    take_profit_price, stop_loss_price, max_hold_seconds,
                    status, opened_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
                """,
                (
                    fields["packet_id"],
                    fields["desk"],
                    fields["asset_id"],
                    fields.get("side", "BUY"),
                    float(fields["entry_price"]),
                    float(fields.get("current_price", fields["entry_price"])),
                    float(fields.get("peak_price", fields["entry_price"])),
                    fields.get("microprice"),
                    fields.get("imbalance"),
                    float(fields["kelly_pct"]),
                    float(fields["notional_usd"]),
                    float(fields["take_profit_price"]),
                    float(fields["stop_loss_price"]),
                    int(fields["max_hold_seconds"]),
                    fields.get("opened_at") or utcnow(),
                ),
            )
            return int(cur.lastrowid or 0)

    def open_fills(self) -> list[sqlite3.Row]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM paper_fills WHERE status='OPEN' ORDER BY opened_at")
            return list(cur.fetchall())

    def close_fill(
        self,
        fill_id: int,
        *,
        exit_price: float,
        exit_reason: str,
        pnl_usd: float,
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                UPDATE paper_fills SET status='CLOSED', current_price=?, exit_reason=?,
                    pnl_usd=?, closed_at=? WHERE fill_id=?
                """,
                (exit_price, exit_reason, pnl_usd, utcnow(), fill_id),
            )

    def update_fill_mark(
        self, fill_id: int, price: float, peak: float, imbalance: float | None = None
    ) -> None:
        with self.cursor() as cur:
            if imbalance is None:
                cur.execute(
                    "UPDATE paper_fills SET current_price=?, peak_price=? WHERE fill_id=?",
                    (price, peak, fill_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE paper_fills SET current_price=?, peak_price=?, imbalance=?
                    WHERE fill_id=?
                    """,
                    (price, peak, imbalance, fill_id),
                )

    def count_open_fills(self) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM paper_fills WHERE status='OPEN'")
            return int(cur.fetchone()[0])

    def count_fills_since(self, iso_day_start: str) -> int:
        with self.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM paper_fills WHERE opened_at >= ?",
                (iso_day_start,),
            )
            return int(cur.fetchone()[0])

    def realized_pnl_since(self, iso_day_start: str) -> float:
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(pnl_usd), 0) FROM paper_fills
                WHERE status='CLOSED' AND closed_at >= ?
                """,
                (iso_day_start,),
            )
            return float(cur.fetchone()[0] or 0)

    def peak_and_equity(self, bankroll: float) -> tuple[float, float]:
        """Return (peak_equity, current_equity) from closed PnL + open MTM approx."""
        with self.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(pnl_usd), 0) FROM paper_fills WHERE status='CLOSED'"
            )
            closed = float(cur.fetchone()[0] or 0)
            cur.execute(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN side='BUY' OR side='YES' OR side='NO'
                    THEN (current_price - entry_price) / NULLIF(entry_price, 0) * notional_usd
                    ELSE 0 END
                ), 0) FROM paper_fills WHERE status='OPEN'
                """
            )
            open_mtm = float(cur.fetchone()[0] or 0)
        equity = bankroll + closed + open_mtm
        # Persist peak via a lightweight pragma table isn't available; approximate peak
        # as max(bankroll, equity) for circuit breaker until an equity table exists.
        peak = max(bankroll, equity)
        return peak, equity

    def sector_exposure(self, sector: str) -> float:
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(notional_usd), 0) FROM paper_fills
                WHERE status='OPEN' AND desk=?
                """,
                (sector,),
            )
            return float(cur.fetchone()[0] or 0)

    # ----- creator memory -----

    def remember_creator(self, deployer: str, mint: str, outcome: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO creator_memory (deployer_wallet, last_token_mint, outcome, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(deployer_wallet) DO UPDATE SET
                    last_token_mint=excluded.last_token_mint,
                    outcome=excluded.outcome,
                    updated_at=excluded.updated_at
                """,
                (deployer, mint, outcome, utcnow()),
            )

    def creator_outcome(self, deployer: str) -> Optional[str]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT outcome FROM creator_memory WHERE deployer_wallet=?",
                (deployer,),
            )
            row = cur.fetchone()
            return None if row is None else str(row["outcome"])


_db: Database | None = None


def get_db(path: Path | str | None = None) -> Database:
    global _db
    if path is not None:
        return Database(path)
    if _db is None:
        STATE.mkdir(parents=True, exist_ok=True)
        _db = Database()
    return _db
