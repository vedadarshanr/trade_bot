"""Deterministic HFT / scalp math. Bots never recompute these by hand."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Desk = Literal["polymarket", "pumpfun"]


def calc_microprice(bid: float, ask: float, q_bid: float, q_ask: float) -> float:
    """Size-weighted microprice: (bid*q_ask + ask*q_bid) / (q_bid + q_ask)."""
    den = max(q_bid + q_ask, 1e-9)
    return (bid * q_ask + ask * q_bid) / den


def calc_imbalance(q_bid: float, q_ask: float) -> float:
    """Queue imbalance in [-1, 1]: (q_bid - q_ask) / (q_bid + q_ask)."""
    den = max(q_bid + q_ask, 1e-9)
    return (q_bid - q_ask) / den


def bayesian_posterior(
    prior: float,
    likelihood_pos: float,
    likelihood_neg: float,
) -> float:
    """Sequential Bayes update for a binary edge hypothesis.

    `likelihood_pos` / `likelihood_neg` already fold smart-money entry odds and
    X-velocity odds into P(evidence|H) and P(evidence|~H). Returns posterior in (0,1).
    """
    prior = min(1.0 - 1e-9, max(1e-9, float(prior)))
    lp = max(1e-12, float(likelihood_pos))
    ln = max(1e-12, float(likelihood_neg))
    numer = prior * lp
    denom = numer + (1.0 - prior) * ln
    return numer / max(denom, 1e-12)


def quarter_kelly_scalp(
    posterior_p: float,
    take_profit_pct: float,
    stop_loss_pct: float,
    *,
    kelly_fraction: float = 0.25,
    hard_cap: float = 0.03,
) -> float:
    """Quarter-Kelly scalp fraction, hard-capped at `hard_cap` (default 3%).

    f_star = p/stop - (1-p)/take
    return min(hard_cap, max(0, kelly_fraction * f_star))
    """
    p = min(1.0 - 1e-9, max(1e-9, float(posterior_p)))
    tp = max(1e-9, abs(float(take_profit_pct)))
    sl = max(1e-9, abs(float(stop_loss_pct)))
    f_star = (p / sl) - ((1.0 - p) / tp)
    return min(hard_cap, max(0.0, kelly_fraction * f_star))


@dataclass(frozen=True)
class ScalpTargets:
    take_profit_price: float
    stop_loss_price: float
    trailing_stop_pct: float
    time_stop_seconds: int
    take_profit_pct: float
    stop_loss_pct: float


def compute_scalp_targets(
    desk: Desk | str,
    entry_price: float,
    microprice: float,
    imbalance: float,
    volatility: float,
) -> ScalpTargets:
    """Exact TP/SL/trail/time-stop so bots never guess exit prices.

    Polymarket: +4c..+12c TP, -3c SL, 30m dead-catalyst stop.
    Pump.fun: +45% full TP (partial marker +25%), -12% SL, -10% trail, 12m time-stop.
    """
    entry = float(entry_price)
    vol = max(0.0, float(volatility))
    imb = float(imbalance)
    edge = float(microprice) - entry

    if desk == "polymarket":
        # Map volatility (typical 0.01–0.08 on binary books) into 4–12¢ TP.
        tp_cents = 0.04 + min(0.08, vol * 1.5 + max(0.0, edge) * 0.5)
        tp_cents = min(0.12, max(0.04, tp_cents))
        # Strong imbalance slightly widens TP toward the upper bound.
        if imb >= 0.40:
            tp_cents = min(0.12, tp_cents + 0.02)
        sl_cents = 0.03
        take = entry + tp_cents
        stop = entry - sl_cents
        return ScalpTargets(
            take_profit_price=round(take, 6),
            stop_loss_price=round(max(0.01, stop), 6),
            trailing_stop_pct=0.0,
            time_stop_seconds=30 * 60,
            take_profit_pct=tp_cents / max(entry, 1e-9),
            stop_loss_pct=sl_cents / max(entry, 1e-9),
        )

    # pumpfun defaults from SCALP_PLAYBOOK
    tp_pct = 0.45
    sl_pct = 0.12
    trail = 0.10
    take = entry * (1.0 + tp_pct)
    stop = entry * (1.0 - sl_pct)
    return ScalpTargets(
        take_profit_price=round(take, 12),
        stop_loss_price=round(max(0.0, stop), 12),
        trailing_stop_pct=trail,
        time_stop_seconds=12 * 60,
        take_profit_pct=tp_pct,
        stop_loss_pct=sl_pct,
    )


def stage6_total(
    audit_and_copy_score: float,
    x_narrative_velocity_score: float,
    macro_timing_score: float,
    microstructure_quant_score: float,
) -> float:
    """Locked Stage-6 consensus formula from AGENTS.md."""
    return (
        0.30 * float(audit_and_copy_score)
        + 0.25 * float(x_narrative_velocity_score)
        + 0.15 * float(macro_timing_score)
        + 0.30 * float(microstructure_quant_score)
    )


def mid_price(bid: float, ask: float) -> float:
    return (float(bid) + float(ask)) / 2.0
