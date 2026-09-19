from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("NOVA_STATE_DIR", ROOT / "nova-state"))
LEDGER = STATE_DIR / "nova-life-history-v1.jsonl"
EVOLUTION = STATE_DIR / "nova-evolution-state-v1.json"


def fmt_duration(seconds):
    if seconds is None:
        return "?"
    total = max(0, int(round(float(seconds))))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


def fmt_pos(record):
    pos = (record.get("final_state") or {}).get("position") or {}
    if not pos:
        return "(?, ?, ?)"
    return f"({pos.get('x','?')},{pos.get('y','?')},{pos.get('z','?')})"


def load_records():
    if not LEDGER.exists():
        return []
    records = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("event") == "life_terminal":
            records.append(item)
    return records


def main():
    records = load_records()
    if not records:
        print("Nova life ledger is empty.")
    else:
        for record in records:
            actions = len(record.get("last_actions") or [])
            print(
                f"Life {record.get('life_number','?')} "
                f"[{str(record.get('life_id',''))[:8]}]: "
                f"{fmt_duration(record.get('duration_seconds'))}  "
                f"{record.get('terminal_state','?'):<7} "
                f"pos={fmt_pos(record)}  actions={actions}"
            )

    state = {}
    if EVOLUTION.exists():
        try:
            state = json.loads(EVOLUTION.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}

    print()
    print(
        "Next life:",
        state.get("next_life_number", state.get("life_number", 1)),
        "| completed deaths:",
        state.get("lives_completed", 0),
    )


if __name__ == "__main__":
    main()
