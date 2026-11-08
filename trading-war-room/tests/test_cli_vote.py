import json
from pathlib import Path

import pytest

from war_room_engine import cli_vote
from war_room_engine.db import Database


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "war_room.db"
    database = Database(path)
    monkeypatch.setattr(cli_vote, "get_db", lambda: database)
    monkeypatch.setattr(cli_vote, "HANDOFF_PATH", tmp_path / "handoff_notes.md")
    monkeypatch.setattr(cli_vote, "EVIDENCE_PATH", tmp_path / "PIPELINE-EVIDENCE.jsonl")
    return database


def _seed_packet(db: Database, packet_id: str = "pkt_test1") -> dict:
    packet = {
        "packet_id": packet_id,
        "desk": "polymarket",
        "asset_id": "0xabc",
        "ticker_or_slug": "test-market",
        "smart_money_signal": True,
        "x_velocity_score": 70,
        "stage2_metrics_score": 80,
        "kelly_pct": 0.02,
        "side": "YES",
        "entry_price": 0.50,
        "take_profit_price": 0.56,
        "stop_loss_price": 0.47,
        "max_hold_seconds": 1800,
        "microstructure": {"imbalance": 0.35, "microprice": 0.51},
        "status": "pending",
    }
    db.upsert_candidate(packet)
    return packet


def test_list_pending(db, capsys):
    _seed_packet(db)
    assert cli_vote.main(["list-pending"]) == 0
    out = capsys.readouterr().out
    assert "pkt_test1" in out


def test_vote_and_stage6(db, capsys):
    _seed_packet(db)
    assert (
        cli_vote.main(
            [
                "vote",
                "--packet",
                "pkt_test1",
                "--seat",
                "OnChain-Auditor",
                "--json",
                json.dumps({"audit_and_copy_score": 80, "approve": True}),
            ]
        )
        == 0
    )
    assert (
        cli_vote.main(
            [
                "vote",
                "--packet",
                "pkt_test1",
                "--seat",
                "Narrative-Scout",
                "--json",
                json.dumps(
                    {
                        "x_narrative_velocity_score": 70,
                        "macro_timing_score": 60,
                    }
                ),
            ]
        )
        == 0
    )
    assert (
        cli_vote.main(
            [
                "vote",
                "--packet",
                "pkt_test1",
                "--seat",
                "Quant-Pricer",
                "--json",
                json.dumps({"microstructure_quant_score": 75, "kelly_pct": 0.02}),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Stage-6 Total_Score" in out
    row = db.get_candidate("pkt_test1")
    # 0.30*80 + 0.25*70 + 0.15*60 + 0.30*75 = 24+17.5+9+22.5 = 73.0
    assert abs(float(row["stage6_total_score"]) - 73.0) < 1e-6


def test_execute_paper_fail_closed_without_adversarial(db):
    _seed_packet(db)
    for seat, payload in (
        ("OnChain-Auditor", {"audit_and_copy_score": 90, "approve": True}),
        (
            "Narrative-Scout",
            {"x_narrative_velocity_score": 80, "macro_timing_score": 70},
        ),
        ("Quant-Pricer", {"microstructure_quant_score": 80}),
    ):
        cli_vote.main(
            ["vote", "--packet", "pkt_test1", "--seat", seat, "--json", json.dumps(payload)]
        )
    # No adversarial vote → deny
    assert cli_vote.main(["execute-paper", "--packet", "pkt_test1"]) == 1
    assert db.count_open_fills() == 0


def test_execute_paper_success(db, tmp_path):
    _seed_packet(db)
    for seat, payload in (
        ("OnChain-Auditor", {"audit_and_copy_score": 90, "approve": True}),
        (
            "Narrative-Scout",
            {"x_narrative_velocity_score": 80, "macro_timing_score": 70},
        ),
        ("Quant-Pricer", {"microstructure_quant_score": 80}),
        ("Adversarial-Checker", {"approve": True, "veto_reasons": []}),
    ):
        assert (
            cli_vote.main(
                ["vote", "--packet", "pkt_test1", "--seat", seat, "--json", json.dumps(payload)]
            )
            == 0
        )
    assert cli_vote.main(["execute-paper", "--packet", "pkt_test1"]) == 0
    assert db.count_open_fills() == 1
    assert (tmp_path / "handoff_notes.md").exists()


def test_invalid_json_fail_closed(db, capsys):
    _seed_packet(db)
    rc = cli_vote.main(
        ["vote", "--packet", "pkt_test1", "--seat", "Adversarial-Checker", "--json", "{not-json"]
    )
    assert rc == 0  # recorded deny vote
    votes = db.votes_for("pkt_test1")
    assert votes["Adversarial-Checker"]["approve"] is False
