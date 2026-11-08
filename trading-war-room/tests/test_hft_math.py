from war_room_engine.hft_math import (
    bayesian_posterior,
    calc_imbalance,
    calc_microprice,
    compute_scalp_targets,
    quarter_kelly_scalp,
    stage6_total,
)


def test_microprice_balanced():
    assert abs(calc_microprice(0.40, 0.42, 100, 100) - 0.41) < 1e-9


def test_microprice_ask_heavy_pulls_toward_bid():
    # More ask size → microprice closer to bid.
    mp = calc_microprice(0.40, 0.50, 10, 90)
    assert mp < 0.45


def test_imbalance_range():
    assert calc_imbalance(100, 0) == 1.0
    assert calc_imbalance(0, 100) == -1.0
    assert abs(calc_imbalance(50, 50)) < 1e-12


def test_bayesian_posterior_moves_with_evidence():
    base = bayesian_posterior(0.5, 1.0, 1.0)
    assert abs(base - 0.5) < 1e-9
    bull = bayesian_posterior(0.5, 2.0, 0.5)
    assert bull > 0.5
    bear = bayesian_posterior(0.5, 0.5, 2.0)
    assert bear < 0.5


def test_quarter_kelly_cap():
    # Extremely favorable → still capped at 3%.
    f = quarter_kelly_scalp(0.95, take_profit_pct=0.10, stop_loss_pct=0.03)
    assert 0.0 <= f <= 0.03
    # Coin-flip with symmetric odds → near zero / zero.
    f2 = quarter_kelly_scalp(0.5, 0.10, 0.10)
    assert f2 == 0.0


def test_polymarket_targets():
    t = compute_scalp_targets("polymarket", 0.50, 0.52, 0.30, 0.03)
    assert 0.54 <= t.take_profit_price <= 0.62
    assert abs(t.stop_loss_price - 0.47) < 1e-6
    assert t.time_stop_seconds == 1800


def test_pumpfun_targets():
    t = compute_scalp_targets("pumpfun", 1.0, 1.0, 0.2, 0.2)
    assert abs(t.take_profit_price - 1.45) < 1e-9
    assert abs(t.stop_loss_price - 0.88) < 1e-9
    assert t.trailing_stop_pct == 0.10
    assert t.time_stop_seconds == 720


def test_stage6_weights():
    total = stage6_total(100, 100, 100, 100)
    assert abs(total - 100.0) < 1e-9
    total2 = stage6_total(100, 0, 0, 0)
    assert abs(total2 - 30.0) < 1e-9
