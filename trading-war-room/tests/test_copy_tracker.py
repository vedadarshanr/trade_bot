import httpx
import pytest

from war_room_engine.copy_tracker import CopyTracker
from war_room_engine.db import Database
from war_room_engine.paths import SMART_WALLETS_MD


@pytest.mark.asyncio
async def test_leaderboard_intersection_and_md(tmp_path, monkeypatch):
    db = Database(tmp_path / "w.db")
    md_path = tmp_path / "SMART_WALLETS.md"
    monkeypatch.setattr("war_room_engine.copy_tracker.SMART_WALLETS_MD", md_path)

    week = [
        {"user_id": "0xaaa", "pnl": 1200, "winRate": 0.62, "volume": 50_000, "user_name": "Alpha"},
        {"user_id": "0xbbb", "pnl": 80, "winRate": 0.55, "volume": 200_000, "user_name": "Farmer"},
        {"user_id": "0xccc", "pnl": -10, "winRate": 0.4, "volume": 10_000, "user_name": "Loser"},
    ]
    month = [
        {"user_id": "0xaaa", "pnl": 5000, "winRate": 0.60, "volume": 200_000, "user_name": "Alpha"},
        {"user_id": "0xbbb", "pnl": 90, "winRate": 0.52, "volume": 800_000, "user_name": "Farmer"},
        {"user_id": "0xddd", "pnl": 900, "winRate": 0.7, "volume": 20_000, "user_name": "MonthOnly"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        period = request.url.params.get("time_period")
        rows = week if period == "week" else month
        return httpx.Response(200, json={"data": rows, "pagination": {}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        async with CopyTracker(db=db, http=http) as tracker:
            top = await tracker.refresh_polymarket_leaderboard()

    # 0xaaa present both windows with real PnL; 0xbbb filtered as farmer; 0xccc/0xddd not in ∩ with profit
    assert any(r["wallet_address"] == "0xaaa" for r in top)
    assert all(r["wallet_address"] != "0xbbb" for r in top)
    assert md_path.exists()
    text = md_path.read_text()
    assert "0xaaa" in text
    assert "Table A" in text
    assert "Table B" in text


def test_copy_score_monotonic():
    low = CopyTracker._copy_score(100, 200, 0.50)
    high = CopyTracker._copy_score(5000, 20000, 0.70)
    assert high > low
    assert 0 <= low <= 100 and 0 <= high <= 100
