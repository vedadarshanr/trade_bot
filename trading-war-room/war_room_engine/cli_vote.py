#!/usr/bin/env python3
"""Single-command bot interface & Stage-6 consensus aggregator."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Allow `python3 war_room_engine/cli_vote.py` from repo root.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from war_room_engine.db import get_db, utcnow  # noqa: E402
from war_room_engine.decisions import load_decisions, reload_decisions  # noqa: E402
from war_room_engine.hft_math import stage6_total  # noqa: E402
from war_room_engine.paths import EVIDENCE_PATH, HANDOFF_PATH  # noqa: E402

SEATS = {
    "Chief-Dispatcher",
    "OnChain-Auditor",
    "Narrative-Scout",
    "Quant-Pricer",
    "Adversarial-Checker",
    "Risk-Execution-Bot",
}

SCORE_SEATS = ("OnChain-Auditor", "Narrative-Scout", "Quant-Pricer")


def _load_packet_json(row_packet_json: str) -> dict[str, Any]:
    try:
        return json.loads(row_packet_json)
    except json.JSONDecodeError:
        return {}


def cmd_list_pending(_: argparse.Namespace) -> int:
    db = get_db()
    rows = db.list_pending_candidates()
    # Also surface JSONL lines not yet voted.
    if not rows and EVIDENCE_PATH.exists():
        # Backfill from evidence file if DB empty.
        for line in EVIDENCE_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pkt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if pkt.get("status", "pending") == "pending" and pkt.get("packet_id"):
                db.upsert_candidate(pkt)
        rows = db.list_pending_candidates()

    if not rows:
        print("No pending packets.")
        return 0

    print(f"{'PACKET_ID':<22} {'DESK':<11} {'ASSET':<24} {'XVEL':>6} {'S2':>6} {'SM':>3}")
    print("-" * 80)
    for r in rows:
        print(
            f"{r['packet_id']:<22} {r['desk']:<11} {str(r['asset_id'])[:24]:<24} "
            f"{r['x_velocity_score']:>6.1f} {r['stage2_metrics_score']:>6.1f} "
            f"{'Y' if r['smart_money_signal'] else 'N':>3}"
        )
    print(f"\n{len(rows)} pending. Vote with:")
    print("  python3 war_room_engine/cli_vote.py vote --packet <ID> --seat <SEAT> --json '...'")
    return 0


def _extract_scores(seat: str, payload: dict[str, Any]) -> tuple[Optional[float], Optional[bool]]:
    approve = payload.get("approve")
    if approve is not None:
        approve = bool(approve)
    score = None
    if seat == "OnChain-Auditor":
        score = payload.get("audit_and_copy_score", payload.get("score"))
    elif seat == "Narrative-Scout":
        # Prefer narrative velocity; timing stored separately in payload.
        score = payload.get("x_narrative_velocity_score", payload.get("score"))
    elif seat == "Quant-Pricer":
        score = payload.get("microstructure_quant_score", payload.get("score"))
    elif seat == "Adversarial-Checker":
        score = 100.0 if approve else 0.0
    elif "score" in payload:
        score = payload.get("score")
    try:
        score_f = float(score) if score is not None else None
    except (TypeError, ValueError):
        score_f = None
        approve = False
    return score_f, approve


def _compute_stage6(votes: dict[str, dict[str, Any]]) -> Optional[float]:
    if not all(s in votes for s in SCORE_SEATS):
        return None
    audit = votes["OnChain-Auditor"]["payload"]
    narr = votes["Narrative-Scout"]["payload"]
    quant = votes["Quant-Pricer"]["payload"]
    try:
        a = float(audit.get("audit_and_copy_score", votes["OnChain-Auditor"]["score"] or 0))
        n = float(
            narr.get("x_narrative_velocity_score", votes["Narrative-Scout"]["score"] or 0)
        )
        t = float(narr.get("macro_timing_score", 50.0))
        q = float(
            quant.get("microstructure_quant_score", votes["Quant-Pricer"]["score"] or 0)
        )
    except (TypeError, ValueError):
        return 0.0
    return round(stage6_total(a, n, t, q), 4)


def cmd_vote(args: argparse.Namespace) -> int:
    seat = args.seat
    if seat not in SEATS:
        print(f"Unknown seat {seat!r}. Valid: {sorted(SEATS)}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(args.json)
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON (fail-closed): {exc}", file=sys.stderr)
        # Still record a deny vote if possible.
        payload = {"seat": seat, "approve": False, "error": "invalid_json"}
    if not isinstance(payload, dict):
        print("JSON payload must be an object (fail-closed).", file=sys.stderr)
        payload = {"seat": seat, "approve": False, "error": "not_object"}

    payload.setdefault("seat", seat)
    # Fail-closed: missing required score fields for scoring seats.
    score, approve = _extract_scores(seat, payload)
    if seat in SCORE_SEATS and score is None:
        print(f"{seat}: missing score field — recording approve=false", file=sys.stderr)
        approve = False
        score = 0.0
        payload["approve"] = False
    if seat == "Adversarial-Checker" and "approve" not in payload:
        payload["approve"] = False
        approve = False

    db = get_db()
    cand = db.get_candidate(args.packet)
    if cand is None:
        print(f"Unknown packet_id {args.packet}", file=sys.stderr)
        return 1

    db.upsert_vote(args.packet, seat, score, approve, payload)
    votes = db.votes_for(args.packet)
    total = _compute_stage6(votes)
    if total is not None:
        db.set_candidate_status(args.packet, cand["status"], stage6=total)
        print(f"Stage-6 Total_Score = {total:.2f}")
        decisions = load_decisions()
        if total < decisions.min_stage6_score:
            print(
                f"Below MIN_STAGE6_SCORE={decisions.min_stage6_score} — execution blocked."
            )
    else:
        missing = [s for s in SCORE_SEATS if s not in votes]
        print(f"Vote recorded. Waiting on: {', '.join(missing)}")

    print(json.dumps({"ok": True, "seat": seat, "packet_id": args.packet, "stage6": total}))
    return 0


def _day_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _risk_gates(db, packet: dict[str, Any], votes: dict[str, dict[str, Any]]) -> tuple[bool, list[str]]:
    decisions = reload_decisions()
    reasons: list[str] = []

    total = packet.get("stage6_total_score")
    if total is None:
        total = _compute_stage6(votes)
    if total is None or float(total) < decisions.min_stage6_score:
        reasons.append(f"stage6_score<{decisions.min_stage6_score}")

    adv = votes.get("Adversarial-Checker")
    if not adv or not adv.get("approve"):
        reasons.append("adversarial_deny")

    if db.count_open_fills() >= decisions.max_open_positions:
        reasons.append("max_open_positions")

    if db.count_fills_since(_day_start_iso()) >= decisions.max_daily_trades:
        reasons.append("max_daily_trades")

    day_pnl = db.realized_pnl_since(_day_start_iso())
    if day_pnl <= -decisions.daily_loss_limit_pct * decisions.paper_bankroll_usd:
        reasons.append("daily_loss_limit")

    peak, equity = db.peak_and_equity(decisions.paper_bankroll_usd)
    if peak > 0 and (peak - equity) / peak >= decisions.drawdown_circuit_breaker_pct:
        reasons.append("drawdown_circuit_breaker")

    desk = str(packet.get("desk") or "")
    sector_exp = db.sector_exposure(desk)
    kelly = float(packet.get("kelly_pct") or 0)
    notional = min(
        decisions.max_notional_usd,
        decisions.paper_bankroll_usd * max(0.0, kelly),
    )
    if notional <= 0:
        notional = decisions.max_notional_usd * 0.25
    if (sector_exp + notional) / decisions.paper_bankroll_usd > decisions.max_sector_concentration_pct:
        reasons.append("sector_concentration")

    imb = float((packet.get("microstructure") or {}).get("imbalance") or packet.get("imbalance") or 0)
    if imb <= decisions.max_adverse_imbalance:
        reasons.append("adverse_imbalance")

    if decisions.live_trading_enabled and not decisions.dry_run:
        reasons.append("live_blocked_use_paper")

    # Attach computed size for execute path
    packet["_gate_notional"] = notional
    packet["_gate_stage6"] = total
    return len(reasons) == 0, reasons


def cmd_execute_paper(args: argparse.Namespace) -> int:
    db = get_db()
    cand = db.get_candidate(args.packet)
    if cand is None:
        print(f"Unknown packet_id {args.packet}", file=sys.stderr)
        return 1
    packet = _load_packet_json(cand["packet_json"])
    packet["stage6_total_score"] = cand["stage6_total_score"]
    votes = db.votes_for(args.packet)

    ok, reasons = _risk_gates(db, packet, votes)
    if not ok:
        print(f"FAIL-CLOSED risk gates: {reasons}", file=sys.stderr)
        db.set_candidate_status(args.packet, "rejected")
        return 1

    decisions = load_decisions()
    entry = float(packet.get("entry_price") or 0)
    tp = float(packet.get("take_profit_price") or 0)
    sl = float(packet.get("stop_loss_price") or 0)
    hold = int(packet.get("max_hold_seconds") or 1800)
    kelly = float(packet.get("kelly_pct") or 0)
    notional = float(packet.get("_gate_notional") or decisions.max_notional_usd)
    micro = (packet.get("microstructure") or {}).get("microprice", entry)
    imb = (packet.get("microstructure") or {}).get("imbalance", 0)

    fill_id = db.open_fill(
        packet_id=args.packet,
        desk=packet.get("desk", "unknown"),
        asset_id=packet.get("asset_id", ""),
        side=packet.get("side", "BUY"),
        entry_price=entry,
        current_price=entry,
        peak_price=entry,
        microprice=micro,
        imbalance=imb,
        kelly_pct=kelly,
        notional_usd=notional,
        take_profit_price=tp,
        stop_loss_price=sl,
        max_hold_seconds=hold,
    )
    db.set_candidate_status(args.packet, "executed", stage6=packet.get("_gate_stage6"))

    note = (
        f"## Paper fill `{fill_id}` — {utcnow()}\n"
        f"- packet: `{args.packet}`\n"
        f"- desk: `{packet.get('desk')}` asset: `{packet.get('asset_id')}`\n"
        f"- side: `{packet.get('side')}` entry: `{entry}` TP: `{tp}` SL: `{sl}`\n"
        f"- kelly: `{kelly:.4f}` notional_usd: `{notional:.2f}` stage6: `{packet.get('_gate_stage6')}`\n"
        f"- max_hold_seconds: `{hold}`\n\n"
    )
    HANDOFF_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HANDOFF_PATH.open("a", encoding="utf-8") as fh:
        fh.write(note)

    print(json.dumps({"ok": True, "fill_id": fill_id, "notional_usd": notional}))
    return 0


def cmd_run_exit_cycle(_: argparse.Namespace) -> int:
    """Mark-to-market open fills and close on playbook exits (paper marks)."""
    db = get_db()
    closed = []
    now = datetime.now(timezone.utc).timestamp()
    for fill in db.open_fills():
        entry = float(fill["entry_price"])
        price = float(fill["current_price"])
        peak = float(fill["peak_price"])
        imb = float(fill["imbalance"] or 0)
        desk = fill["desk"]
        # Paper daemon without live marks: drift price slightly toward TP for demo
        # stability — real marks come from scanners when wired. Use packet targets only.
        # Re-evaluate using stored fields; allow optional --mark via env later.
        reason = None
        if desk == "polymarket":
            if price <= float(fill["stop_loss_price"]):
                reason = "stop_loss"
            elif imb < -0.45:
                reason = "imbalance_reversal"
            elif price >= float(fill["take_profit_price"]):
                reason = "take_profit"
            else:
                opened = datetime.fromisoformat(fill["opened_at"]).timestamp()
                if now - opened >= int(fill["max_hold_seconds"]) and abs(price - entry) < 0.015:
                    reason = "time_stop_dead_catalyst"
                elif now - opened >= int(fill["max_hold_seconds"]):
                    reason = "time_stop"
        else:  # pumpfun
            if price <= float(fill["stop_loss_price"]):
                reason = "stop_loss"
            elif price >= float(fill["take_profit_price"]):
                reason = "take_profit"
            else:
                trail = 0.10
                if peak > 0 and price <= peak * (1.0 - trail) and price > entry:
                    reason = "trailing_stop"
                opened = datetime.fromisoformat(fill["opened_at"]).timestamp()
                if now - opened >= int(fill["max_hold_seconds"]):
                    reason = "time_stop_volume_stall"

        # Synthetic mark update: pull mid toward microprice memory if present
        mark = float(fill["microprice"] or price)
        if mark != price:
            peak = max(peak, mark)
            db.update_fill_mark(fill["fill_id"], mark, peak, imb)
            price = mark

        # Re-check after mark
        if reason is None and desk == "polymarket":
            if price <= float(fill["stop_loss_price"]):
                reason = "stop_loss"
            elif price >= float(fill["take_profit_price"]):
                reason = "take_profit"
        elif reason is None and desk == "pumpfun":
            if price <= float(fill["stop_loss_price"]):
                reason = "stop_loss"
            elif price >= float(fill["take_profit_price"]):
                reason = "take_profit"

        if reason:
            if entry > 0:
                pnl = (price - entry) / entry * float(fill["notional_usd"])
                if fill["side"] in ("NO", "SELL"):
                    pnl = (entry - price) / entry * float(fill["notional_usd"])
            else:
                pnl = 0.0
            db.close_fill(fill["fill_id"], exit_price=price, exit_reason=reason, pnl_usd=pnl)
            closed.append({"fill_id": fill["fill_id"], "reason": reason, "pnl_usd": pnl})
            with HANDOFF_PATH.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"## Closed fill `{fill['fill_id']}` — {utcnow()}\n"
                    f"- reason: `{reason}` pnl_usd: `{pnl:.4f}`\n\n"
                )

    print(json.dumps({"ok": True, "closed": closed, "still_open": db.count_open_fills()}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cli_vote", description="War room bot vote CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list-pending", help="List packets awaiting votes")
    sp.set_defaults(func=cmd_list_pending)

    sp = sub.add_parser("vote", help="Submit a seat vote JSON")
    sp.add_argument("--packet", required=True)
    sp.add_argument("--seat", required=True)
    sp.add_argument("--json", required=True)
    sp.set_defaults(func=cmd_vote)

    sp = sub.add_parser("execute-paper", help="Open paper scalp after gates pass")
    sp.add_argument("--packet", required=True)
    sp.set_defaults(func=cmd_execute_paper)

    sp = sub.add_parser("run-exit-cycle", help="Evaluate OPEN fills against exits")
    sp.set_defaults(func=cmd_run_exit_cycle)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
