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
import shutil
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
VALIDATION_STATE = BRIDGE / "validation-state"
VALIDATION_LOGS = BRIDGE / "validation-logs"


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

    if BRIDGE.resolve() == (ROOT / "nova-ipc").resolve():
        raise ValidationFailure(
            "Refusing destructive lifecycle validation on the normal nova-ipc directory. "
            "Use START_NOVA_VALIDATION.cmd and a disposable character."
        )

    add_check(checks, "validation guard + observe", lambda: (
        "bridge reports validation_mode=true"
        if assert_validation_mode()
        else "unreachable"
    ))

    shutil.rmtree(VALIDATION_STATE, ignore_errors=True)
    shutil.rmtree(VALIDATION_LOGS, ignore_errors=True)
    VALIDATION_STATE.mkdir(parents=True, exist_ok=True)
    VALIDATION_LOGS.mkdir(parents=True, exist_ok=True)

    # Import the real controller against isolated validation state.  This makes
    # the lifecycle test exercise the production lesson/evolution code without
    # touching Life 1's durable files.
    os.environ["NOVA_STATE_DIR"] = str(VALIDATION_STATE)
    os.environ["NOVA_LOG_DIR"] = str(VALIDATION_LOGS)
    os.environ["NOVA_BRIDGE_DIR"] = str(BRIDGE)
    import nova_agent_beta as agent

    context: dict[str, object] = {}

    def prepare_life_one():
        hunger = send("validation_set_hunger", value=100)
        require_success(hunger, {"validation_value_set"})
        thirst = send("validation_set_thirst", value=100)
        require_success(thirst, {"validation_value_set"})
        observed = send("observe")
        require_success(observed, {"observed"})
        state = state_from(observed)
        if not bool(state.get("validation_mode")):
            raise ValidationFailure("validation mode disappeared before lifecycle setup")

        evolution = agent.load_evolution_state()
        life = agent.ensure_current_life(evolution, state)
        seeded_action = {
            "action": "wait_one_turn",
            "label": "deterministic lifecycle lesson seed",
        }
        life.record_action(
            state, state, seeded_action, "waited",
            {"evidence": "validation_seed_action"},
        )
        life.observe(state, 1, force_sample=True)
        agent.write_active_life_marker(life)

        context["state"] = state
        context["evolution"] = evolution
        context["life"] = life
        return (
            f"isolated Life 1 created as {life.life_id[:8]} with deterministic "
            "last_action=wait_one_turn and critical hunger/thirst"
        )

    add_check(checks, "prepare isolated Life 1 lesson conditions", prepare_life_one)

    def kill_and_verify_native():
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
        context["death_status"] = status
        return f"native death and life-status propagation verified ({status.get('status')})"

    add_check(checks, "forced deterministic native death", kill_and_verify_native)

    def process_death_to_lesson():
        evolution = context.get("evolution")
        life = context.get("life")
        if not isinstance(evolution, dict) or life is None:
            raise ValidationFailure("lifecycle setup context is missing")

        log_path = VALIDATION_LOGS / "lifecycle-validation.jsonl"
        processed = agent.recover_previous_runtime(evolution, log_path)
        if not processed:
            raise ValidationFailure("production recover_previous_runtime did not consume the death")

        history = agent.load_recent_life_history(limit=20, include_aborted=True)
        lessons = agent.load_lessons(limit=20)
        matching_records = [
            record for record in history
            if record.get("life_id") == life.life_id and record.get("terminal_state") == "dead"
        ]
        matching_lessons = [
            lesson for lesson in lessons
            if lesson.get("source_life_id") == life.life_id
        ]
        if len(matching_records) != 1:
            raise ValidationFailure(
                f"expected exactly one dead terminal record, found {len(matching_records)}"
            )
        if len(matching_lessons) != 1:
            raise ValidationFailure(
                f"expected exactly one death lesson, found {len(matching_lessons)}"
            )
        lesson = matching_lessons[0]
        if lesson.get("at_death_action") != "wait_one_turn":
            raise ValidationFailure(
                f"lesson recorded wrong death action: {lesson.get('at_death_action')!r}"
            )

        conditions = lesson.get("conditions") or {}
        expected_conditions = {
            "hunger_critical": True,
            "thirst_critical": True,
            "indoors": bool(context["state"].get("indoors")),
        }
        mismatches = {
            key: {"expected": expected, "actual": conditions.get(key)}
            for key, expected in expected_conditions.items()
            if conditions.get(key) is not expected
        }
        if mismatches:
            raise ValidationFailure(
                f"lesson conditions are not specific/correct: {mismatches}; full={conditions}"
            )
        lesson_text = str(lesson.get("text") or "")
        expected_text_fragments = [
            "Life ended while taking wait_one_turn",
            "hunger was critical",
            "thirst was critical",
            "Nova was indoors" if expected_conditions["indoors"] else "Nova was outdoors",
        ]
        missing_fragments = [
            fragment for fragment in expected_text_fragments
            if fragment not in lesson_text
        ]
        if missing_fragments:
            raise ValidationFailure(
                "lesson text is too vague or missing expected evidence; "
                f"missing={missing_fragments}, lesson={lesson_text!r}"
            )

        evolved = agent.load_evolution_state()
        if int(evolved.get("lives_completed", 0) or 0) != 1:
            raise ValidationFailure(f"lives_completed is not 1: {evolved}")
        if int(evolved.get("next_life_number", 0) or 0) != 2:
            raise ValidationFailure(f"next_life_number is not 2: {evolved}")

        context["log_path"] = log_path
        context["lesson"] = lesson
        context["evolved"] = evolved
        return (
            f"death produced exactly one specific lesson {lesson.get('lesson_id')}: "
            f"{lesson_text} | advanced evolution state to Life 2"
        )

    add_check(checks, "death -> terminal record -> lesson", process_death_to_lesson)

    def duplicate_death_guard():
        evolved = context.get("evolved")
        log_path = context.get("log_path")
        if not isinstance(evolved, dict) or not isinstance(log_path, Path):
            raise ValidationFailure("processed death context is missing")

        consumed = load_json(agent.CONSUMED_LIFE_STATUS_PATH)
        status = consumed.get("status")
        if not isinstance(status, dict):
            raise ValidationFailure("consumed death status was not archived")
        agent.write_json_atomic(agent.LIFE_STATUS_PATH, status)

        history_before = len(agent.load_recent_life_history(limit=100, include_aborted=True))
        lessons_before = len(agent.load_lessons(limit=100))
        processed = agent.recover_previous_runtime(evolved, log_path)
        if not processed:
            raise ValidationFailure("duplicate death signature was not recognized")
        history_after = len(agent.load_recent_life_history(limit=100, include_aborted=True))
        lessons_after = len(agent.load_lessons(limit=100))
        if history_after != history_before or lessons_after != lessons_before:
            raise ValidationFailure(
                "duplicate death created an extra terminal record or lesson "
                f"(history {history_before}->{history_after}, lessons {lessons_before}->{lessons_after})"
            )
        return "duplicate death signature was consumed without duplicating history or lessons"

    add_check(checks, "exactly-once duplicate death guard", duplicate_death_guard)

    def life_two_inheritance_and_visible_memory():
        original = context.get("state")
        evolved = context.get("evolved")
        lesson = context.get("lesson")
        log_path = context.get("log_path")
        if not isinstance(original, dict) or not isinstance(evolved, dict) or not isinstance(lesson, dict):
            raise ValidationFailure("inheritance context is missing")
        if not isinstance(log_path, Path):
            raise ValidationFailure("lifecycle log path is missing")

        next_state = dict(original)
        next_state["dead"] = False
        # Keep the critical hunger/thirst and indoor/outdoor context identical so
        # the derived lesson has a deterministic >=3-condition match.
        life2 = agent.ensure_current_life(evolved, next_state)
        if life2.life_number != 2:
            raise ValidationFailure(f"expected Life 2, got Life {life2.life_number}")
        life1 = context.get("life")
        if life1 is not None and life2.life_id == life1.life_id:
            raise ValidationFailure("Life 2 incorrectly reused Life 1 identity")

        candidate_actions = [{
            "action": "wait_one_turn",
            "label": "matching action for inherited lesson test",
            "controller_score": 0.80,
        }]
        biased, matches = agent.apply_lesson_bias(
            next_state, candidate_actions, agent.load_lessons(limit=20)
        )
        lesson_id = str(lesson.get("lesson_id"))
        matched_ids = {str(m.get("lesson_id")) for m in matches}
        if lesson_id not in matched_ids:
            raise ValidationFailure(
                f"Life 2 did not retrieve/match inherited lesson {lesson_id}; matches={matches}"
            )
        if not biased or float(biased[0].get("controller_score", 1.0)) >= 0.80:
            raise ValidationFailure(f"lesson matched but did not reduce action score: {biased}")
        bias = biased[0].get("lesson_bias") or {}
        if lesson_id not in {str(x) for x in bias.get("lesson_ids") or []}:
            raise ValidationFailure(f"biased action does not cite inherited lesson: {biased[0]}")

        wm = agent.WorldModel()
        wm.observe(next_state)
        feed = agent.ThoughtFeed()
        dashboard = agent.DashboardFeed()
        priority_context = {"tier": 4, "name": "validation", "reason": "lifecycle test"}
        fired = agent.publish_lesson_signal(
            wm, matches, feed, log_path, life2, priority_context, next_state
        )
        if not fired:
            raise ValidationFailure("production lesson signal helper did not fire for Life 2")
        dashboard.update(next_state, wm, None, matches)

        thought_text = (agent.BRIDGE / "nova-thoughts.txt").read_text(
            encoding="utf-8", errors="replace"
        )
        status_text = (agent.BRIDGE / "nova-status.txt").read_text(
            encoding="utf-8", errors="replace"
        )
        expected_memory_fragments = [
            lesson_id,
            "wait_one_turn",
            "critical hunger",
            "critical thirst",
            "indoors" if bool(next_state.get("indoors")) else "outdoors",
        ]
        missing_thought = [
            fragment for fragment in expected_memory_fragments
            if fragment not in thought_text
        ]
        missing_status = [
            fragment for fragment in expected_memory_fragments
            if fragment not in status_text
        ]
        if missing_thought:
            raise ValidationFailure(
                "visible thought feed fired MEMORY but did not show specific inherited "
                f"lesson content; missing={missing_thought}; text={thought_text!r}"
            )
        if missing_status:
            raise ValidationFailure(
                "Nova Status MEMORY line did not show specific inherited lesson "
                f"content; missing={missing_status}; text={status_text!r}"
            )

        log_lines = log_path.read_text(encoding="utf-8").splitlines()
        fired_events = []
        for line in log_lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("session_event") == "lesson_fired":
                fired_events.append(payload)
        if not fired_events:
            raise ValidationFailure("no lesson_fired event was written to cognition log")

        return (
            f"Life 2 ({life2.life_id[:8]}) inherited {lesson_id}, action score "
            f"{candidate_actions[0]['controller_score']:.2f}->{float(biased[0]['controller_score']):.2f}, "
            "and visible MEMORY/log signals showed the specific inherited conditions"
        )

    add_check(
        checks,
        "Life 2 lesson inheritance + behavioral bias + visible MEMORY",
        life_two_inheritance_and_visible_memory,
    )
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
