"""Canonical filesystem paths for the war room."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state"
DB_PATH = STATE / "war_room.db"
EVIDENCE_PATH = STATE / "PIPELINE-EVIDENCE.jsonl"
MACRO_PULSE_PATH = STATE / "macro_pulse.json"
HANDOFF_PATH = STATE / "handoff_notes.md"
SMART_WALLETS_MD = ROOT / "SMART_WALLETS.md"
DECISIONS_MD = ROOT / "DECISIONS.md"
AGENTS_MD = ROOT / "AGENTS.md"
SCALP_PLAYBOOK_MD = ROOT / "SCALP_PLAYBOOK.md"

STATE.mkdir(parents=True, exist_ok=True)
