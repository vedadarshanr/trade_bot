"""Unified Pump.fun + Polymarket pre-filter & packet emitter."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from .copy_tracker import CopyTracker
from .db import Database, get_db, utcnow
from .decisions import load_decisions
from .hft_math import (
    calc_imbalance,
    calc_microprice,
    compute_scalp_targets,
    mid_price,
    quarter_kelly_scalp,
    bayesian_posterior,
)
from .paths import EVIDENCE_PATH
from .x_sentiment import (
    extract_text_signals,
    has_social_metadata,
    score_velocity,
)

log = logging.getLogger(__name__)

GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK = "https://clob.polymarket.com/book"
DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens"
IPFS_GATEWAYS = (
    "https://ipfs.io/ipfs/",
    "https://cf-ipfs.com/ipfs/",
)
PUMP_WS = "wss://pumpportal.fun/api/data"

PACKET_RATE_WINDOW = 300.0  # 5 minutes
MAX_PACKETS_PER_WINDOW = 2
DEDUP_COOLDOWN = 2 * 3600  # 2 hours


@dataclass
class PendingPumpToken:
    mint: str
    created_at: float
    creator: str = ""
    symbol: str = ""
    name: str = ""
    uri: str = ""
    buyers: set[str] = field(default_factory=set)
    sol_in_curve: float = 0.0
    bonding_curve_pct: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


class PacketEmitter:
    def __init__(self, db: Database | None = None, evidence_path: Path | None = None) -> None:
        self.db = db or get_db()
        self.evidence_path = evidence_path or EVIDENCE_PATH
        self.evidence_path.parent.mkdir(parents=True, exist_ok=True)
        self._emitted_at: dict[str, float] = {}
        self._recent_emits: deque[float] = deque()

    def _rate_ok(self) -> bool:
        now = time.time()
        while self._recent_emits and now - self._recent_emits[0] > PACKET_RATE_WINDOW:
            self._recent_emits.popleft()
        return len(self._recent_emits) < MAX_PACKETS_PER_WINDOW

    def _cooldown_ok(self, asset_id: str) -> bool:
        last = self._emitted_at.get(asset_id, 0.0)
        if time.time() - last < DEDUP_COOLDOWN:
            return False
        # Survive daemon restarts: consult SQLite for a recent candidate.
        with self.db.cursor() as cur:
            cur.execute(
                """
                SELECT created_at FROM candidates
                WHERE asset_id=? ORDER BY created_at DESC LIMIT 1
                """,
                (asset_id,),
            )
            row = cur.fetchone()
        if row and row["created_at"]:
            try:
                from datetime import datetime

                ts = datetime.fromisoformat(str(row["created_at"])).timestamp()
                if time.time() - ts < DEDUP_COOLDOWN:
                    self._emitted_at[asset_id] = ts
                    return False
            except ValueError:
                pass
        return True

    def emit(self, packet: dict[str, Any]) -> bool:
        asset_id = str(packet.get("asset_id") or "")
        if not asset_id or not self._rate_ok() or not self._cooldown_ok(asset_id):
            return False
        decisions = load_decisions()
        if (
            packet.get("desk") == "polymarket"
            and asset_id in decisions.vetoed_polymarket_conditions
        ):
            return False
        if (
            packet.get("desk") == "pumpfun"
            and packet.get("creator") in decisions.blacklisted_solana_deployers
        ):
            return False

        packet.setdefault("packet_id", f"pkt_{uuid.uuid4().hex[:16]}")
        packet.setdefault("created_at", utcnow())
        packet.setdefault("status", "pending")

        line = json.dumps(packet, ensure_ascii=False, default=str)
        with self.evidence_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self.db.upsert_candidate(packet)
        self._emitted_at[asset_id] = time.time()
        self._recent_emits.append(time.time())
        log.info(
            "emitted packet %s desk=%s asset=%s",
            packet["packet_id"],
            packet.get("desk"),
            asset_id[:16],
        )
        return True


class PolymarketScanner:
    def __init__(
        self,
        emitter: PacketEmitter,
        tracker: CopyTracker,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.emitter = emitter
        self.tracker = tracker
        self.http = http

    async def scan_once(self) -> int:
        assert self.http is not None
        emitted = 0
        try:
            r = await self.http.get(
                GAMMA_MARKETS,
                params={"active": "true", "closed": "false", "limit": 200},
            )
            r.raise_for_status()
            markets = r.json()
        except Exception as exc:
            log.warning("gamma markets failed: %s", exc)
            return 0
        if not isinstance(markets, list):
            return 0

        for m in markets:
            if not isinstance(m, dict):
                continue
            packet = await self._evaluate_market(m)
            if packet and self.emitter.emit(packet):
                emitted += 1
                if emitted >= MAX_PACKETS_PER_WINDOW:
                    break
        return emitted

    async def _evaluate_market(self, m: dict[str, Any]) -> Optional[dict[str, Any]]:
        assert self.http is not None
        liquidity = float(m.get("liquidityNum") or m.get("liquidity") or 0)
        volume = float(m.get("volume24hr") or m.get("volumeNum") or m.get("volume") or 0)
        if liquidity < 10_000 or volume < 5_000:
            return None

        condition_id = str(m.get("conditionId") or m.get("id") or "")
        if not condition_id:
            return None

        token_ids = self._token_ids(m)
        if len(token_ids) < 1:
            return None

        book = await self._fetch_book(token_ids[0])
        if not book:
            return None
        bid, ask, q_bid, q_ask = book
        if ask <= bid or (ask - bid) > 0.04:
            return None

        micro = calc_microprice(bid, ask, q_bid, q_ask)
        mid = mid_price(bid, ask)
        imb = calc_imbalance(q_bid, q_ask)
        edge_cents = (micro - mid) * 100.0

        yes_price = float(m.get("outcomePrices", "[]") and 0)
        try:
            prices = m.get("outcomePrices")
            if isinstance(prices, str):
                prices = json.loads(prices)
            if isinstance(prices, list) and prices:
                yes_price = float(prices[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            yes_price = mid

        no_ask = 1.0 - bid  # approx from yes bid
        safe_compounder = yes_price <= 0.20 and no_ask > 0.80 and (no_ask - 0.80) > 0.05

        hits = self.tracker.hits_for_asset(condition_id, within_seconds=300)
        # Also match by slug
        slug = str(m.get("slug") or "")
        if slug:
            hits += self.tracker.hits_for_asset(slug, within_seconds=300)
        smart_money = len(hits) > 0

        trigger = None
        if smart_money:
            trigger = "smart_money_entry"
        elif abs(imb) >= 0.28 and abs(micro - mid) >= 0.015:
            trigger = "orderbook_imbalance"
        elif safe_compounder:
            trigger = "safe_compounder"
        else:
            return None
        if trigger == "orderbook_imbalance" and imb < 0.25 and not safe_compounder:
            # Playbook wants imbalance >= 0.25 with micro leading mid for catalyst path.
            if not (imb >= 0.25 and micro > mid):
                return None

        text = extract_text_signals(m)
        sentiment = score_velocity(
            mention_acceleration=0.4 if smart_money else 0.15,
            social_links_present=False,
            text_blob=text,
            smart_money_convergence=len(hits),
            price_move_pct=abs(edge_cents) / 100.0,
            catalyst_started_at=time.time() - (60 if smart_money else 300),
        )

        prior = 0.50
        lp = 1.8 if smart_money else 1.1
        ln = 0.7 if sentiment.x_velocity_score >= 60 else 1.0
        if sentiment.exhaustion_risk == "HIGH":
            ln *= 1.4
        posterior = bayesian_posterior(prior, lp, ln)
        vol = max(0.01, (ask - bid) * 2)
        targets = compute_scalp_targets("polymarket", mid, micro, imb, vol)
        kelly = quarter_kelly_scalp(
            posterior, targets.take_profit_pct, targets.stop_loss_pct
        )
        stage2 = min(
            100.0,
            40.0
            + 20.0 * min(1.0, liquidity / 50_000)
            + 20.0 * min(1.0, volume / 25_000)
            + 20.0 * max(0.0, 1.0 - (ask - bid) / 0.04),
        )

        side = "YES" if imb >= 0 else "NO"
        if safe_compounder:
            side = "NO"

        return {
            "packet_id": f"pkt_{uuid.uuid4().hex[:16]}",
            "desk": "polymarket",
            "asset_id": condition_id,
            "ticker_or_slug": slug or str(m.get("question") or "")[:80],
            "question": m.get("question"),
            "smart_money_signal": smart_money,
            "smart_money_wallets": [h.wallet for h in hits],
            "trigger": trigger,
            "x_velocity_score": sentiment.x_velocity_score,
            "exhaustion_risk": sentiment.exhaustion_risk,
            "macro_timing_score": sentiment.macro_timing_score,
            "stage2_metrics_score": round(stage2, 2),
            "microstructure": {
                "bid": bid,
                "ask": ask,
                "q_bid": q_bid,
                "q_ask": q_ask,
                "mid_price": mid,
                "microprice": micro,
                "imbalance": imb,
                "edge_cents": edge_cents,
                "spread": ask - bid,
            },
            "bayesian_posterior": posterior,
            "kelly_pct": kelly,
            "side": side,
            "entry_price": mid,
            "take_profit_price": targets.take_profit_price,
            "stop_loss_price": targets.stop_loss_price,
            "trailing_stop_pct": targets.trailing_stop_pct,
            "max_hold_seconds": targets.time_stop_seconds,
            "liquidity": liquidity,
            "volume_24h": volume,
            "status": "pending",
            "created_at": utcnow(),
        }

    async def _fetch_book(
        self, token_id: str
    ) -> Optional[tuple[float, float, float, float]]:
        assert self.http is not None
        try:
            r = await self.http.get(CLOB_BOOK, params={"token_id": token_id})
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            log.debug("book %s failed: %s", token_id[:8], exc)
            return None
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        if not bids or not asks:
            return None
        try:
            best_bid = max(bids, key=lambda x: float(x.get("price") or x[0]))
            best_ask = min(asks, key=lambda x: float(x.get("price") or x[0]))
            bid = float(best_bid.get("price") if isinstance(best_bid, dict) else best_bid[0])
            ask = float(best_ask.get("price") if isinstance(best_ask, dict) else best_ask[0])
            q_bid = float(best_bid.get("size") if isinstance(best_bid, dict) else best_bid[1])
            q_ask = float(best_ask.get("size") if isinstance(best_ask, dict) else best_ask[1])
        except (TypeError, ValueError, IndexError, KeyError):
            return None
        return bid, ask, q_bid, q_ask

    @staticmethod
    def _token_ids(m: dict[str, Any]) -> list[str]:
        raw = m.get("clobTokenIds") or m.get("tokens") or []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return []
        if isinstance(raw, list):
            out = []
            for t in raw:
                if isinstance(t, dict):
                    out.append(str(t.get("token_id") or t.get("tokenId") or ""))
                else:
                    out.append(str(t))
            return [x for x in out if x]
        return []


class PumpfunScanner:
    def __init__(
        self,
        emitter: PacketEmitter,
        tracker: CopyTracker,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.emitter = emitter
        self.tracker = tracker
        self.http = http
        self.pending: dict[str, PendingPumpToken] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="pump-scanner")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        try:
            import websockets
        except ImportError:
            log.warning("websockets missing; pump scanner idle")
            return
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    PUMP_WS, ping_interval=20, open_timeout=20
                ) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    log.info("pumpfun scanner connected")
                    backoff = 1.0
                    last_sweep = time.time()
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=10)
                            await self._on_msg(ws, raw)
                        except asyncio.TimeoutError:
                            pass
                        if time.time() - last_sweep >= 10:
                            last_sweep = time.time()
                            await self._sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("pump scanner reconnect in %.0fs (%s)", backoff, exc)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(60.0, backoff * 2)

    async def _on_msg(self, ws: Any, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        tx = payload.get("txType") or payload.get("type")
        mint = str(payload.get("mint") or "")
        if tx in ("create", "created") and mint:
            tok = PendingPumpToken(
                mint=mint,
                created_at=time.time(),
                creator=str(payload.get("traderPublicKey") or payload.get("creator") or ""),
                symbol=str(payload.get("symbol") or ""),
                name=str(payload.get("name") or ""),
                uri=str(payload.get("uri") or payload.get("metadataUri") or ""),
                bonding_curve_pct=float(payload.get("vSolInBondingCurve") or 0) / 85.0
                if payload.get("vSolInBondingCurve")
                else 0.0,
                sol_in_curve=float(payload.get("vSolInBondingCurve") or 0),
            )
            self.pending[mint] = tok
            try:
                await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
            except Exception:
                pass
            return
        if mint and mint in self.pending and tx in ("buy", "sell"):
            wallet = str(payload.get("traderPublicKey") or payload.get("wallet") or "")
            if tx == "buy" and wallet:
                self.pending[mint].buyers.add(wallet)
            if payload.get("vSolInBondingCurve") is not None:
                self.pending[mint].sol_in_curve = float(payload["vSolInBondingCurve"])
                self.pending[mint].bonding_curve_pct = min(
                    1.0, self.pending[mint].sol_in_curve / 85.0
                )

    async def _sweep(self) -> None:
        now = time.time()
        for mint, tok in list(self.pending.items()):
            age = now - tok.created_at
            if age < 120:
                continue
            if len(tok.buyers) < 5 or tok.bonding_curve_pct >= 0.40:
                if age > 900:
                    self.pending.pop(mint, None)
                continue
            meta = await self._resolve_metadata(tok)
            if not meta.get("name") and not tok.name:
                if age > 600:
                    self.pending.pop(mint, None)
                continue
            creator_pct, top5_pct, risk = self._stage2_metrics(tok, meta)
            if creator_pct >= 0.25 or top5_pct >= 0.80 or risk > 7.0:
                self.pending.pop(mint, None)
                continue

            hits = self.tracker.hits_for_asset(mint, within_seconds=300)
            text = extract_text_signals({**meta, "name": tok.name, "symbol": tok.symbol})
            sentiment = score_velocity(
                mention_acceleration=0.5 if hits else 0.25,
                social_links_present=has_social_metadata(meta),
                text_blob=text,
                smart_money_convergence=len(hits),
                catalyst_started_at=tok.created_at,
            )
            if not hits and sentiment.x_velocity_score < 55:
                # Need smart-money OR velocity spike
                if age > 600:
                    self.pending.pop(mint, None)
                continue
            if sentiment.exhaustion_risk == "HIGH":
                self.pending.pop(mint, None)
                continue

            entry = max(1e-12, float(meta.get("price") or tok.sol_in_curve / 1e9 or 1e-8))
            targets = compute_scalp_targets("pumpfun", entry, entry, 0.3, 0.2)
            posterior = bayesian_posterior(
                0.5, 1.6 if hits else 1.2, 0.8 if sentiment.x_velocity_score >= 60 else 1.0
            )
            kelly = quarter_kelly_scalp(
                posterior, targets.take_profit_pct, targets.stop_loss_pct
            )
            stage2_score = max(0.0, 100.0 - risk * 10.0)

            packet = {
                "packet_id": f"pkt_{uuid.uuid4().hex[:16]}",
                "desk": "pumpfun",
                "asset_id": mint,
                "ticker_or_slug": tok.symbol or mint[:8],
                "creator": tok.creator,
                "smart_money_signal": bool(hits),
                "smart_money_wallets": [h.wallet for h in hits],
                "trigger": "smart_money_entry" if hits else "x_velocity_spike",
                "x_velocity_score": sentiment.x_velocity_score,
                "exhaustion_risk": sentiment.exhaustion_risk,
                "macro_timing_score": sentiment.macro_timing_score,
                "stage2_metrics_score": round(stage2_score, 2),
                "stage1": {
                    "buyers": len(tok.buyers),
                    "bonding_curve_pct": tok.bonding_curve_pct,
                    "age_seconds": age,
                    "metadata_resolved": True,
                },
                "stage2": {
                    "risk_score": risk,
                    "creator_holding_pct": creator_pct,
                    "top5_holders_pct": top5_pct,
                },
                "bayesian_posterior": posterior,
                "kelly_pct": kelly,
                "side": "BUY",
                "entry_price": entry,
                "take_profit_price": targets.take_profit_price,
                "stop_loss_price": targets.stop_loss_price,
                "trailing_stop_pct": targets.trailing_stop_pct,
                "max_hold_seconds": targets.time_stop_seconds,
                "partial_take_profit_pct": 0.25,
                "status": "pending",
                "created_at": utcnow(),
            }
            if self.emitter.emit(packet):
                self.pending.pop(mint, None)

    async def _resolve_metadata(self, tok: PendingPumpToken) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "name": tok.name,
            "symbol": tok.symbol,
            "uri": tok.uri,
        }
        uri = tok.uri
        if uri.startswith("ipfs://"):
            cid = uri[len("ipfs://") :]
            for gw in IPFS_GATEWAYS:
                try:
                    assert self.http is not None
                    r = await self.http.get(gw + cid, timeout=8.0)
                    if r.status_code < 400:
                        data = r.json()
                        if isinstance(data, dict):
                            meta.update(data)
                            break
                except Exception:
                    continue
        elif uri.startswith("http") and self.http is not None:
            try:
                r = await self.http.get(uri, timeout=8.0)
                if r.status_code < 400:
                    data = r.json()
                    if isinstance(data, dict):
                        meta.update(data)
            except Exception:
                pass
        if self.http is not None:
            try:
                r = await self.http.get(f"{DEXSCREENER}/{tok.mint}", timeout=8.0)
                if r.status_code < 400:
                    data = r.json()
                    pairs = data.get("pairs") if isinstance(data, dict) else None
                    if isinstance(pairs, list) and pairs:
                        p0 = pairs[0]
                        meta.setdefault("name", (p0.get("baseToken") or {}).get("name"))
                        meta.setdefault("symbol", (p0.get("baseToken") or {}).get("symbol"))
                        if p0.get("priceUsd"):
                            meta["price"] = float(p0["priceUsd"])
            except Exception:
                pass
        tok.meta = meta
        if meta.get("name"):
            tok.name = str(meta["name"])
        if meta.get("symbol"):
            tok.symbol = str(meta["symbol"])
        return meta

    def _stage2_metrics(
        self, tok: PendingPumpToken, meta: dict[str, Any]
    ) -> tuple[float, float, float]:
        # Without a holders API key we estimate conservatively from buyer count.
        n = max(1, len(tok.buyers))
        creator_pct = 0.10 if n >= 10 else 0.20
        top5_pct = min(0.79, 0.15 + 2.0 / n)
        risk = 3.0
        if not has_social_metadata(meta):
            risk += 1.5
        if tok.bonding_curve_pct > 0.30:
            risk += 1.0
        if n < 8:
            risk += 1.0
        if self.emitter.db.creator_outcome(tok.creator) == "rug":
            risk = 10.0
            creator_pct = 0.50
        return creator_pct, top5_pct, min(10.0, risk)
