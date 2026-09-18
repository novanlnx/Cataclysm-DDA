from __future__ import annotations

import json
import os
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib import request

ROOT = Path(__file__).resolve().parent
BRIDGE = Path(os.environ.get("NOVA_BRIDGE_DIR", ROOT / "nova-ipc"))
LOG_DIR = ROOT / "nova-logs"
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
RUN_MINUTES = int(os.environ.get("NOVA_RUN_MINUTES", "60"))
MODEL_OVERRIDE = os.environ.get("NOVA_MODEL", "").strip()
MAX_HISTORY = 12

CARDINALS = {
    "north": (0, -1),
    "south": (0, 1),
    "west": (-1, 0),
    "east": (1, 0),
}

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def write_json_atomic(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)

def send_command(action: str, timeout: float = 180.0, **kwargs) -> dict:
    BRIDGE.mkdir(parents=True, exist_ok=True)
    command_path = BRIDGE / "command.json"
    command_id = uuid.uuid4().hex
    response_path = BRIDGE / f"response-{command_id}.json"
    payload = {"id": command_id, "action": action, **kwargs}
    if command_path.exists():
        command_path.unlink()
    if response_path.exists():
        response_path.unlink()
    write_json_atomic(command_path, payload)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if response_path.exists():
            try:
                data = json.loads(response_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                time.sleep(0.05)
                continue
            if data.get("id") == command_id:
                try:
                    response_path.unlink()
                except OSError:
                    pass
                return data
        time.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for CDDA response to {action} ({command_id})")

def ollama_json(path: str, payload: dict | None = None, timeout: float = 180.0) -> dict:
    url = OLLAMA + path
    if payload is None:
        req = request.Request(url, method="GET")
    else:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def pick_model() -> str | None:
    if MODEL_OVERRIDE:
        return MODEL_OVERRIDE
    try:
        tags = ollama_json("/api/tags", timeout=5.0).get("models", [])
    except Exception:
        return None
    names = [m.get("name", "") for m in tags if m.get("name")]
    if not names:
        return None
    preferred = [n for n in names if "qwen" in n.lower() and "7b" in n.lower()]
    if preferred:
        return preferred[0]
    qwen = [n for n in names if "qwen" in n.lower()]
    return qwen[0] if qwen else names[0]

def state_from_response(resp: dict) -> dict:
    return resp.get("after") or resp.get("before") or {}

def tile_map(state: dict) -> dict[tuple[int, int], dict]:
    result = {}
    for tile in state.get("local_tiles", []):
        try:
            result[(int(tile["dx"]), int(tile["dy"]))] = tile
        except Exception:
            continue
    return result

def hostile_positions(state: dict) -> set[tuple[int, int]]:
    out = set()
    for c in state.get("nearby_creatures", []):
        if str(c.get("attitude", "")).lower() == "hostile":
            try:
                out.add((int(c["dx"]), int(c["dy"])))
            except Exception:
                pass
    return out

def available_actions(state: dict) -> list[dict]:
    actions = [{"action": "wait_one_turn", "label": "wait briefly"}]
    tiles = tile_map(state)
    hostiles = hostile_positions(state)
    for name, (dx, dy) in CARDINALS.items():
        tile = tiles.get((dx, dy))
        if not tile:
            continue
        if tile.get("passable"):
            actions.append({
                "action": "move_one_tile", "dx": dx, "dy": dy,
                "label": f"move {name}",
                "terrain": tile.get("terrain", ""),
                "hostile_on_tile": (dx, dy) in hostiles,
            })
        elif tile.get("openable"):
            actions.append({
                "action": "open_adjacent", "dx": dx, "dy": dy,
                "label": f"open {name}", "terrain": tile.get("terrain", "")
            })
    consumables = state.get("inventory_consumables", [])
    if any(int(x.get("nutrition", 0) or 0) > 0 for x in consumables):
        actions.append({"action": "eat_best_food", "label": "eat the best safe carried food"})
    if any(int(x.get("quench", 0) or 0) > 0 for x in consumables):
        actions.append({"action": "drink_best", "label": "drink the best safe carried drink"})
    actions.append({"action": "sleep", "duration_minutes": 480, "label": "try to sleep"})
    return actions

def deterministic_safety(state: dict, actions: list[dict]):
    def find(name: str):
        return next((a for a in actions if a.get("action") == name), None)
    thirst = int(state.get("thirst", 0) or 0)
    hunger = int(state.get("hunger", 0) or 0)
    sleepy = int(state.get("sleepiness", 0) or 0)
    stamina = int(state.get("stamina", 0) or 0)
    stamina_max = max(1, int(state.get("stamina_max", 1) or 1))
    drink = find("drink_best")
    eat = find("eat_best_food")
    sleep = find("sleep")
    wait = find("wait_one_turn")
    if thirst > 80 and drink:
        return drink, f"safety: thirst {thirst} > 80"
    if hunger > 100 and eat:
        return eat, f"safety: hunger {hunger} > 100"
    if sleepy >= 383 and sleep:
        return sleep, f"safety: sleepiness {sleepy} >= 383"
    if stamina / stamina_max < 0.25 and wait:
        return wait, f"safety: stamina below 25% ({stamina}/{stamina_max})"
    return None

def compact_world(state: dict, actions: list[dict], history: list[dict]) -> dict:
    local = []
    for t in state.get("local_tiles", []):
        if abs(int(t.get("dx", 99))) <= 1 and abs(int(t.get("dy", 99))) <= 1:
            local.append({k: t.get(k) for k in ("dx", "dy", "terrain", "passable", "openable", "items")})
    return {
        "needs": {k: state.get(k) for k in (
            "hunger", "thirst", "sleepiness", "stamina", "stamina_max",
            "pain", "morale", "stored_kcal", "healthy_kcal"
        )},
        "position": state.get("position"),
        "activity": state.get("activity"),
        "nearby_tiles": local,
        "nearby_creatures": state.get("nearby_creatures", []),
        "carried_consumables": state.get("inventory_consumables", []),
        "allowed_actions": actions,
        "recent_history": history[-MAX_HISTORY:],
    }

def normalize_choice(raw: dict, allowed: list[dict]):
    action = raw.get("action")
    if not isinstance(action, str):
        return None
    for candidate in allowed:
        if candidate.get("action") != action:
            continue
        if action in {"move_one_tile", "open_adjacent"}:
            try:
                dx = int(raw.get("dx"))
                dy = int(raw.get("dy"))
            except Exception:
                continue
            if dx != candidate.get("dx") or dy != candidate.get("dy"):
                continue
        out = dict(candidate)
        out["reason"] = str(raw.get("reason", ""))[:300]
        try:
            out["score"] = float(raw.get("score", 0.5) or 0.5)
        except Exception:
            out["score"] = 0.5
        if action == "sleep" and "duration_minutes" in raw:
            try:
                out["duration_minutes"] = max(10, min(720, int(raw["duration_minutes"])))
            except Exception:
                pass
        return out
    return None

def qwen_choices(model: str, state: dict, actions: list[dict], history: list[dict]):
    world = compact_world(state, actions, history)
    system = (
        "You are Nova controlling one survivor in Cataclysm: Dark Days Ahead. "
        "Choose only from allowed_actions. Keep the survivor alive, avoid obvious danger, "
        "and make coherent exploratory choices. Moving into a hostile creature can become an attack. "
        "Return JSON only. Propose up to 3 plausible choices. Scores are 0..1. "
        "Do not invent capabilities. Schema: "
        '{"intention":"short phrase","choices":[{"action":"exact action","dx":0,"dy":0,'
        '"duration_minutes":480,"score":0.7,"reason":"short grounded reason"}]}'
    )
    payload = {
        "model": model, "stream": False, "format": "json",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(world, separators=(",", ":"))},
        ],
        "options": {"temperature": 0.35},
    }
    data = ollama_json("/api/chat", payload, timeout=180.0)
    parsed = json.loads(data.get("message", {}).get("content", "{}"))
    intention = str(parsed.get("intention", ""))[:200]
    choices = []
    for raw in parsed.get("choices", [])[:3]:
        if isinstance(raw, dict):
            valid = normalize_choice(raw, actions)
            if valid:
                choices.append(valid)
    return choices, intention

def choose_with_variation(choices: list[dict]):
    if not choices:
        return None
    choices = sorted(choices, key=lambda x: float(x.get("score", 0.0)), reverse=True)
    top = float(choices[0].get("score", 0.0))
    close = [c for c in choices if float(c.get("score", 0.0)) >= top - 0.15]
    weights = [max(0.05, float(c.get("score", 0.1))) for c in close]
    return random.choices(close, weights=weights, k=1)[0]

def fallback_choice(state: dict, actions: list[dict]):
    hostiles = hostile_positions(state)
    moves = [a for a in actions if a.get("action") == "move_one_tile"
             and (a.get("dx"), a.get("dy")) not in hostiles]
    if moves:
        return random.choice(moves), "fallback: random safe-looking movement"
    waits = [a for a in actions if a.get("action") == "wait_one_turn"]
    return (waits[0] if waits else actions[0]), "fallback: wait"

def append_log(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

def command_kwargs(choice: dict) -> dict:
    return {k: choice[k] for k in ("dx", "dy", "duration_minutes") if k in choice}

def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    model = pick_model()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"nova-cdda-run-{stamp}.jsonl"
    history = []

    print("NOVA CDDA SURVIVAL ALPHA")
    print(f"Bridge: {BRIDGE}")
    print(f"Run target: {RUN_MINUTES} minutes")
    print(f"Ollama model: {model or 'NOT FOUND - deterministic fallback will be used'}")
    print(f"Log: {log_path}")
    print()

    try:
        obs = send_command("observe", timeout=30.0)
    except Exception as exc:
        print(f"Cannot reach CDDA bridge: {exc}")
        return 2

    state = state_from_response(obs)
    deadline = time.monotonic() + RUN_MINUTES * 60
    action_count = 0

    while time.monotonic() < deadline:
        actions = available_actions(state)
        safety = deterministic_safety(state, actions)
        intention = ""
        model_error = None

        if safety:
            choice, reason = safety
        else:
            choice = None
            reason = ""
            if model:
                try:
                    options, intention = qwen_choices(model, state, actions, history)
                    choice = choose_with_variation(options)
                    if choice:
                        reason = choice.get("reason", "qwen choice")
                except Exception as exc:
                    model_error = repr(exc)
            if not choice:
                choice, reason = fallback_choice(state, actions)

        action = choice["action"]
        started = time.monotonic()
        try:
            result = send_command(action, timeout=900.0, **command_kwargs(choice))
            command_error = None
        except Exception as exc:
            result = None
            command_error = repr(exc)

        action_count += 1
        record = {
            "wall_time": utc_now(), "action_index": action_count, "model": model,
            "intention": intention, "selected": choice, "reason": reason,
            "model_error": model_error, "command_error": command_error,
            "state_before": state, "result": result,
            "latency_seconds": round(time.monotonic() - started, 3),
        }
        append_log(log_path, record)

        if command_error:
            print(f"[{action_count}] {action}: COMMAND ERROR {command_error}")
            break

        print(f"[{action_count}] {action}: {result.get('outcome')} | {reason[:100]}")

        try:
            obs = send_command("observe", timeout=900.0)
            state = state_from_response(obs)
        except Exception as exc:
            append_log(log_path, {"wall_time": utc_now(), "observe_error": repr(exc)})
            print(f"Observe failed: {exc}")
            break

        history.append({
            "action": action, "outcome": result.get("outcome"), "reason": reason,
            "position": state.get("position"), "hunger": state.get("hunger"),
            "thirst": state.get("thirst"), "sleepiness": state.get("sleepiness"),
        })
        history = history[-MAX_HISTORY:]

        if action_count % 25 == 0:
            try:
                save_result = send_command("quicksave", timeout=120.0)
                append_log(log_path, {"wall_time": utc_now(), "checkpoint": save_result})
                print("  checkpoint saved")
            except Exception as exc:
                append_log(log_path, {"wall_time": utc_now(), "quicksave_error": repr(exc)})

    print()
    print(f"Run finished after {action_count} actions.")
    print(f"Log saved to: {log_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
