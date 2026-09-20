#!/usr/bin/env python3
"""
Deterministic live-game validation harness for Nova.

This does NOT use Qwen and does NOT wait for organic gameplay situations.
It drives the native bridge directly against a throwaway validation character,
creates controlled fixtures through validation-only bridge commands, executes the
capability under test, and verifies the returned before/after state.

Validation fixture commands exist only when CDDA was launched with:
    NOVA_VALIDATION_MODE=1

Never run destructive suites against a real Nova life.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BRIDGE = ROOT / "nova-ipc-validation"
BRIDGE = Path(os.environ.get("NOVA_BRIDGE_DIR", str(DEFAULT_BRIDGE)))
COMMAND = BRIDGE / "command.json"
LIFE_STATUS = BRIDGE / "life-status.json"


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    skipped: bool = False


class ValidationFailure(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def wait_for(predicate: Callable[[], object | None], timeout: float, label: str):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value is not None:
            return value
        time.sleep(0.05)
    raise ValidationFailure(f"Timed out waiting for {label} after {timeout:.1f}s")


def send(action: str, timeout: float = 45.0, **kwargs) -> dict:
    command_id = uuid.uuid4().hex
    response = BRIDGE / f"response-{command_id}.json"
    payload = {"id": command_id, "action": action, **kwargs}

    def command_slot_free():
        return True if not COMMAND.exists() else None

    wait_for(command_slot_free, 10.0, "native command slot")
    if response.exists():
        response.unlink()
    atomic_json(COMMAND, payload)

    def response_ready():
        if not response.exists():
            return None
        try:
            data = load_json(response)
        except (OSError, json.JSONDecodeError):
            return None
        return data if data.get("id") == command_id else None

    data = wait_for(response_ready, timeout, f"response to {action}")
    try:
        response.unlink()
    except OSError:
        pass
    return data


def state_from(response: dict, which: str = "after") -> dict:
    value = response.get(which)
    return value if isinstance(value, dict) else {}


def require_success(response: dict, expected: set[str] | None = None) -> None:
    if not bool(response.get("success")):
        raise ValidationFailure(
            f"{response.get('action')} failed: outcome={response.get('outcome')} "
            f"error={response.get('error')}"
        )
    if expected and response.get("outcome") not in expected:
        raise ValidationFailure(
            f"{response.get('action')} returned unexpected outcome "
            f"{response.get('outcome')!r}; expected {sorted(expected)!r}"
        )


def inventory_item(state: dict, type_id: str) -> dict | None:
    for item in state.get("inventory_items") or []:
        if isinstance(item, dict) and str(item.get("type_id", "")) == type_id:
            return item
    return None


def add_check(checks: list[Check], name: str, fn: Callable[[], str]) -> None:
    try:
        detail = fn()
        checks.append(Check(name=name, passed=True, detail=detail))
        print(f"[PASS] {name}: {detail}")
    except Exception as exc:
        checks.append(Check(name=name, passed=False, detail=str(exc)))
        print(f"[FAIL] {name}: {exc}")


def assert_validation_mode() -> dict:
    response = send("observe")
    require_success(response, {"observed"})
    state = state_from(response)
    if not bool(state.get("validation_mode")):
        raise ValidationFailure(
            "CDDA is not in validation mode. Close it and launch with "
            "NOVA_VALIDATION_MODE=1. Do NOT enable this on Life 1."
        )
    return state


def give_first_valid(candidates: list[str], predicate: Callable[[dict], bool]) -> tuple[str, dict]:
    errors = []
    for type_id in candidates:
        response = send("validation_give_item", item_type_id=type_id)
        if not bool(response.get("success")):
            errors.append(f"{type_id}:{response.get('outcome')}")
            continue
        item = inventory_item(state_from(response), type_id)
        if item and predicate(item):
            return type_id, item
        errors.append(f"{type_id}:fixture exists but affordance predicate false")
    raise ValidationFailure("No suitable fixture item found: " + ", ".join(errors))


def batch1_suite() -> list[Check]:
    checks: list[Check] = []

    add_check(checks, "validation guard + observe", lambda: (
        "bridge reports validation_mode=true"
        if assert_validation_mode()
        else "unreachable"
    ))

    def rich_inventory():
        response = send("validation_give_item", item_type_id="rock")
        require_success(response, {"validation_fixture_created"})
        item = inventory_item(state_from(response), "rock")
        if not item:
            raise ValidationFailure("rock was created but not exposed in inventory_items")
        required = {
            "name", "type_id", "wielded", "worn", "consumable", "armor",
            "gun", "melee", "tool", "container", "usable", "can_wield",
            "can_wear", "weight_grams", "volume_ml", "charges", "count",
            "ammo_remaining",
        }
        missing = sorted(required - set(item))
        if missing:
            raise ValidationFailure(f"rich inventory item is missing fields: {missing}")
        return f"rich inventory schema present for {item.get('name')} ({item.get('type_id')})"

    add_check(checks, "rich inventory perception", rich_inventory)

    def pickup_fixture():
        response = None
        for dx, dy in ((0, 1), (1, 0), (-1, 0), (0, -1)):
            response = send(
                "validation_place_item", item_type_id="rock", dx=dx, dy=dy
            )
            if response.get("success"):
                break
        if not response or not response.get("success"):
            raise ValidationFailure("could not place pickup fixture beside avatar")
        after = state_from(response)
        placed_tile = None
        for tile in after.get("local_tiles") or []:
            if not isinstance(tile, dict):
                continue
            if int(tile.get("dx", 99)) == dx and int(tile.get("dy", 99)) == dy:
                placed_tile = tile
                break
        if not placed_tile or not placed_tile.get("ground_items"):
            raise ValidationFailure("placed fixture is not visible in local ground_items")
        ground = placed_tile["ground_items"][0]
        name = str(ground.get("name", "")).strip()
        if not name:
            raise ValidationFailure("ground fixture has no item name")
        picked = send("pickup_item", dx=dx, dy=dy, item_name=name)
        require_success(picked, {"pickup_verified"})
        return f"placed and picked up {name} through the real pickup action"

    add_check(checks, "generic pickup", pickup_fixture)

    def door_roundtrip():
        selected = None
        errors = []
        for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1)):
            setup = send(
                "validation_set_terrain",
                dx=dx,
                dy=dy,
                terrain_id="t_door_o",
            )
            if setup.get("success"):
                selected = (dx, dy)
                break
            errors.append(f"{dx},{dy}:{setup.get('outcome')}")
        if selected is None:
            raise ValidationFailure("could not create open-door fixture: " + ", ".join(errors))
        dx, dy = selected
        closed = send("close_adjacent", dx=dx, dy=dy)
        require_success(closed, {"closed"})
        opened = send("open_adjacent", dx=dx, dy=dy)
        require_success(opened, {"opened"})
        return f"closed and reopened deterministic door fixture at delta=({dx},{dy})"

    add_check(checks, "door close/open", door_roundtrip)

    def wield_item():
        # The fixture character should normally have fists. If it already has a
        # weapon, use a dedicated fresh validation character rather than mutating
        # a real survivor's equipment.
        observed = send("observe")
        current = next(
            (
                item for item in state_from(observed).get("inventory_items") or []
                if isinstance(item, dict) and bool(item.get("wielded"))
            ),
            None,
        )
        if current:
            raise ValidationFailure(
                f"validation character already wields {current.get('name')}; "
                "use a fresh throwaway validation character"
            )
        type_id, item = give_first_valid(
            ["rock", "hammer", "knife_combat", "2x4"],
            lambda row: bool(row.get("can_wield")),
        )
        response = send(
            "wield_item",
            item_name=item.get("name"),
            item_type_id=type_id,
        )
        require_success(response, {"wield_verified", "already_wielded"})
        post = inventory_item(state_from(response), type_id)
        if not post or not bool(post.get("wielded")):
            raise ValidationFailure("native response succeeded but post-state is not wielded")
        return f"wielded {post.get('name')} and verified post-state"

    add_check(checks, "wield item", wield_item)

    def wear_item():
        type_id, item = give_first_valid(
            ["tshirt", "backpack", "hoodie", "jacket_light"],
            lambda row: bool(row.get("can_wear")),
        )
        response = send(
            "wear_item",
            item_name=item.get("name"),
            item_type_id=type_id,
        )
        require_success(response, {"wear_verified", "already_worn"})
        post = inventory_item(state_from(response), type_id)
        if not post or not bool(post.get("worn")):
            raise ValidationFailure("native response succeeded but post-state is not worn")
        return f"wore {post.get('name')} and verified post-state"

    add_check(checks, "wear item", wear_item)

    def quicksave():
        response = send("quicksave", timeout=60.0)
        require_success(response, {"saved"})
        return "native quicksave returned saved"

    add_check(checks, "quicksave", quicksave)

    def sleep_dispatch():
        setup = send("validation_set_sleepiness", value=100)
        require_success(setup, {"validation_value_set"})
        response = send("sleep", duration_minutes=10, timeout=60.0)
        require_success(response, {"sleep_activity_started"})
        return (
            "sleep activity was started from a controlled fatigue fixture; "
            "long-form sleep completion is a Batch 2 semantic test"
        )

    add_check(checks, "sleep dispatch", sleep_dispatch)
    return checks


def death_suite() -> list[Check]:
    checks: list[Check] = []
    add_check(checks, "validation guard + observe", lambda: (
        "bridge reports validation_mode=true"
        if assert_validation_mode()
        else "unreachable"
    ))

    def kill_and_verify():
        response = send("validation_kill_character", timeout=30.0)
        require_success(response, {"validation_character_killed"})
        if not bool(state_from(response).get("dead")):
            raise ValidationFailure("kill command returned but response post-state is not dead")

        def dead_status():
            if not LIFE_STATUS.exists():
                return None
            try:
                status = load_json(LIFE_STATUS)
            except (OSError, json.JSONDecodeError):
                return None
            return status if status.get("dead") or status.get("status") == "dead" else None

        status = wait_for(dead_status, 15.0, "life-status.json dead state")
        return f"native death and life-status propagation verified ({status.get('status')})"

    add_check(checks, "forced deterministic death", kill_and_verify)
    return checks


def self_test() -> list[Check]:
    checks: list[Check] = []

    def payload_contract():
        sample = {
            "id": "test",
            "action": "validation_give_item",
            "item_type_id": "rock",
        }
        raw = json.dumps(sample, separators=(",", ":"))
        decoded = json.loads(raw)
        if decoded != sample:
            raise ValidationFailure("JSON round-trip mismatch")
        return "command JSON contract round-trips"

    add_check(checks, "validation harness self-test", payload_contract)
    return checks


def write_report(suite: str, checks: list[Check]) -> Path:
    BRIDGE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = BRIDGE / f"validation-report-{suite}-{stamp}.json"
    payload = {
        "schema_version": 1,
        "suite": suite,
        "created_at": utc_now(),
        "bridge_dir": str(BRIDGE),
        "passed": all(c.passed or c.skipped for c in checks),
        "checks": [asdict(c) for c in checks],
    }
    atomic_json(path, payload)
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--suite",
        choices=("batch1", "death"),
        default="batch1",
        help="Live deterministic validation suite to run.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Validate the harness itself without a running game.",
    )
    args = parser.parse_args()

    print("NOVA DETERMINISTIC VALIDATION")
    print(f"Bridge: {BRIDGE}")

    if args.self_test:
        checks = self_test()
        suite = "self-test"
    else:
        if not BRIDGE.exists():
            raise ValidationFailure(
                f"Bridge directory does not exist: {BRIDGE}. "
                "Launch a validation build first."
            )
        checks = batch1_suite() if args.suite == "batch1" else death_suite()
        suite = args.suite

    report = write_report(suite, checks)
    passed = all(c.passed or c.skipped for c in checks)
    print()
    print(f"RESULT: {'PASS' if passed else 'FAIL'}")
    print(f"Report: {report}")
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValidationFailure as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        raise SystemExit(2)
