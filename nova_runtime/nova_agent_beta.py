from __future__ import annotations

import json
import os
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib import request

ROOT = Path(__file__).resolve().parent
BRIDGE = Path(os.environ.get("NOVA_BRIDGE_DIR", ROOT / "nova-ipc"))
LOG_DIR = ROOT / "nova-logs"
STATE_DIR = ROOT / "nova-state"
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
RUN_MINUTES = int(os.environ.get("NOVA_RUN_MINUTES", "60"))
MODEL_OVERRIDE = os.environ.get("NOVA_MODEL", "").strip()
MODEL_AUDIT = os.environ.get("NOVA_MODEL_AUDIT", "").strip().lower() in {"1", "true", "yes", "on"}

CARDINALS = {
    "north": (0, -1),
    "south": (0, 1),
    "west": (-1, 0),
    "east": (1, 0),
}

# Hard validation gate.  No controller path -- Qwen, deterministic safety,
# fallback, or future helper code -- may dispatch outside this set.
VALIDATION_DISPATCH_ALLOWLIST = {
    "observe",
    "move_one_tile",
    "open_adjacent",
    "wait_one_turn",
    "pickup_consumable",
    "eat_best_food",
    "drink_best",
}

MIN_HUNGER_IMPROVEMENT = 5
MIN_THIRST_IMPROVEMENT = 5
MIN_STORED_KCAL_IMPROVEMENT = 10

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    last_error = None
    for _ in range(40):
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.025)
    if last_error:
        raise last_error

def write_json_atomic(path: Path, obj: dict) -> None:
    write_text_atomic(path, json.dumps(obj, ensure_ascii=False, separators=(",", ":")))

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

def ollama_chat_traced(payload: dict, trace_path: Path, timeout: float = 180.0) -> tuple[dict, float]:
    url = OLLAMA + "/api/chat"
    request_body = json.dumps(payload, ensure_ascii=False)
    req = request.Request(
        url,
        data=request_body.encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            response_body = resp.read().decode("utf-8")
            elapsed = time.monotonic() - started
            status = getattr(resp, "status", None)
            append_log(trace_path, {
                "wall_time": utc_now(),
                "endpoint": url,
                "http_method": "POST",
                "request_body": request_body,
                "response_status": status,
                "response_body": response_body,
                "model_latency_seconds": round(elapsed, 3),
            })
            return json.loads(response_body), elapsed
    except Exception as exc:
        elapsed = time.monotonic() - started
        append_log(trace_path, {
            "wall_time": utc_now(),
            "endpoint": url,
            "http_method": "POST",
            "request_body": request_body,
            "response_status": None,
            "response_body": None,
            "model_latency_seconds": round(elapsed, 3),
            "error": repr(exc),
        })
        raise

def pick_model() -> str | None:
    if MODEL_OVERRIDE:
        return MODEL_OVERRIDE
    try:
        tags = ollama_json("/api/tags", timeout=5.0).get("models", [])
    except Exception:
        return None
    names = [m.get("name", "") for m in tags if m.get("name")]
    preferred = [n for n in names if "qwen" in n.lower() and "7b" in n.lower()]
    if preferred:
        return preferred[0]
    qwen = [n for n in names if "qwen" in n.lower()]
    return qwen[0] if qwen else (names[0] if names else None)

def state_from_response(resp: dict) -> dict:
    return resp.get("after") or resp.get("before") or {}

def pos_tuple(state: dict) -> tuple[int, int, int]:
    p = state.get("position") or {}
    return (int(p.get("x", 0)), int(p.get("y", 0)), int(p.get("z", 0)))

def tile_map(state: dict) -> dict[tuple[int, int], dict]:
    out = {}
    for t in state.get("local_tiles", []):
        try:
            out[(int(t["dx"]), int(t["dy"]))] = t
        except Exception:
            pass
    return out

def hostile_positions(state: dict) -> set[tuple[int, int]]:
    out = set()
    for c in state.get("nearby_creatures", []):
        if str(c.get("attitude", "")).lower() == "hostile":
            try:
                out.add((int(c["dx"]), int(c["dy"])))
            except Exception:
                pass
    return out

@dataclass
class WorldModel:
    visits: dict[tuple[int, int, int], int] = field(default_factory=dict)
    known_tiles: dict[tuple[int, int, int], dict] = field(default_factory=dict)
    recent_positions: deque = field(default_factory=lambda: deque(maxlen=10))
    recent_actions: deque = field(default_factory=lambda: deque(maxlen=12))
    seen_hostiles: dict[str, dict] = field(default_factory=dict)
    blocked_edges: set[tuple[int, int, int, int, int, int]] = field(default_factory=set)
    active_goal_id: str = ""
    active_intention: str = ""
    intention_age: int = 0
    progress_epoch: int = 0

    def observe(self, state: dict) -> None:
        p = pos_tuple(state)
        self.visits[p] = self.visits.get(p, 0) + 1
        self.recent_positions.append(p)
        px, py, pz = p
        for t in state.get("local_tiles", []):
            try:
                gx = px + int(t["dx"])
                gy = py + int(t["dy"])
                self.known_tiles[(gx, gy, pz)] = {
                    "terrain": t.get("terrain", ""),
                    "passable": bool(t.get("passable")),
                    "openable": bool(t.get("openable")),
                    "items": list(t.get("items") or []),
                }
            except Exception:
                pass
        for c in state.get("nearby_creatures", []):
            name = str(c.get("name", "creature"))
            self.seen_hostiles[name] = {
                "kind": c.get("kind"),
                "attitude": c.get("attitude"),
                "dx": c.get("dx"), "dy": c.get("dy"),
            }

    def edge_key(self, state: dict, dx: int, dy: int) -> tuple[int, int, int, int, int, int]:
        x, y, z = pos_tuple(state)
        return (x, y, z, x + dx, y + dy, z)

    def is_known_blocked_edge(self, state: dict, dx: int, dy: int) -> bool:
        return self.edge_key(state, dx, dy) in self.blocked_edges

    def record_action(self, state_before: dict, choice: dict, outcome: str) -> None:
        self.recent_actions.append({
            "action": choice.get("action"),
            "dx": choice.get("dx"),
            "dy": choice.get("dy"),
            "outcome": outcome,
        })
        if choice.get("action") == "move_one_tile" and outcome == "blocked":
            try:
                self.blocked_edges.add(
                    self.edge_key(state_before, int(choice.get("dx", 0)), int(choice.get("dy", 0)))
                )
            except Exception:
                pass
        self.intention_age += 1

    def visit_count_target(self, state: dict, dx: int, dy: int) -> int:
        x, y, z = pos_tuple(state)
        return self.visits.get((x + dx, y + dy, z), 0)

    def is_immediate_backtrack(self, state: dict, dx: int, dy: int) -> bool:
        if len(self.recent_positions) < 2:
            return False
        x, y, z = pos_tuple(state)
        return (x + dx, y + dy, z) == self.recent_positions[-2]

    def looping(self) -> bool:
        r = list(self.recent_positions)
        if len(r) < 6:
            return False
        # A-B-A-B or repeated tiny-area pacing.
        if r[-1] == r[-3] == r[-5] and r[-2] == r[-4]:
            return True
        return len(set(r[-8:])) <= 3 and len(r[-8:]) >= 6

class ThoughtFeed:
    def __init__(self) -> None:
        self.lines = deque(maxlen=3)
        self.path = BRIDGE / "nova-thoughts.txt"

    def push(self, text: str) -> None:
        clean = " ".join(str(text).replace("\n", " ").split())
        if len(clean) > 140:
            clean = clean[:137] + "..."
        self.lines.append(clean)
        try:
            write_text_atomic(self.path, "\n".join(self.lines) + "\n")
        except OSError:
            # The in-game panel may momentarily have the file open on Windows.
            # A missed visual refresh must never terminate the agent.
            pass

def situation_summary(state: dict, wm: WorldModel) -> dict:
    tiles = tile_map(state)
    doors = []
    open_moves = []
    for name, (dx, dy) in CARDINALS.items():
        t = tiles.get((dx, dy))
        if not t:
            continue
        if t.get("openable"):
            doors.append({"direction": name, "terrain": t.get("terrain", "")})
        if t.get("passable"):
            open_moves.append(name)

    hostiles = []
    for c in state.get("nearby_creatures", []):
        if str(c.get("attitude", "")).lower() == "hostile":
            hostiles.append({
                "name": c.get("name", "hostile"),
                "dx": c.get("dx"), "dy": c.get("dy")
            })

    visible_items = []
    for t in state.get("local_tiles", []):
        if t.get("items"):
            visible_items.append({
                "dx": t.get("dx"), "dy": t.get("dy"),
                "terrain": t.get("terrain", ""),
                "items": t.get("items")[:3],
                "ground_consumables": (t.get("ground_consumables") or [])[:3],
            })

    return {
        "indoors": bool(state.get("indoors")),
        "adjacent_closed_doors": doors,
        "open_directions": open_moves,
        "visible_items": visible_items[:8],
        "nearby_hostiles": hostiles[:8],
        "current_tile_visits": wm.visits.get(pos_tuple(state), 0),
        "loop_detected": wm.looping(),
        "known_positions": len(wm.visits),
        "known_tiles": len(wm.known_tiles),
    }

def describe_situation(s: dict) -> str:
    parts = []
    parts.append("indoors" if s["indoors"] else "outdoors")
    if s["adjacent_closed_doors"]:
        parts.append("closed door " + "/".join(d["direction"] for d in s["adjacent_closed_doors"]))
    if s["nearby_hostiles"]:
        h = s["nearby_hostiles"][0]
        parts.append(f"hostile {h['name']} nearby")
    if s["visible_items"]:
        names = []
        for group in s["visible_items"][:2]:
            names.extend(group.get("items", [])[:2])
        if names:
            parts.append("items: " + ", ".join(names[:3]))
    if s["loop_detected"]:
        parts.append("repeating path")
    return "; ".join(parts)

def visible_storable_consumables(state: dict) -> list[dict]:
    found = []
    for tile in state.get("local_tiles", []):
        try:
            dx = int(tile.get("dx", 0))
            dy = int(tile.get("dy", 0))
        except Exception:
            continue
        for food in (tile.get("ground_consumables") or []):
            if not bool(food.get("storable_without_wield", False)):
                continue
            name = str(food.get("name", "")).strip()
            if not name:
                continue
            found.append({
                "dx": dx,
                "dy": dy,
                "name": name,
                "nutrition": int(food.get("nutrition", 0) or 0),
                "quench": int(food.get("quench", 0) or 0),
                "distance": abs(dx) + abs(dy),
            })
    return found

def resource_need_weight(state: dict, resource: dict) -> float:
    hunger = max(0, int(state.get("hunger", 0) or 0))
    thirst = max(0, int(state.get("thirst", 0) or 0))
    weight = 0.10  # modest value for stocking a nearby survival resource
    if int(resource.get("quench", 0) or 0) > 0:
        weight += min(0.30, thirst / 240.0)
    if int(resource.get("nutrition", 0) or 0) > 0:
        weight += min(0.25, hunger / 300.0)
    return weight

def goal_candidates(state: dict, actions: list[dict]) -> list[dict]:
    candidates = []
    action_names = {a.get("action") for a in actions}

    if "drink_best" in action_names and int(state.get("thirst", 0) or 0) > 20:
        candidates.append({
            "goal_id": "reduce_thirst",
            "intention": "drink something safe to reduce thirst",
            "supported_by": ["drink_best"],
            "priority": 0.98,
        })
    if "eat_best_food" in action_names and int(state.get("hunger", 0) or 0) > 20:
        candidates.append({
            "goal_id": "reduce_hunger",
            "intention": "eat something safe to reduce hunger",
            "supported_by": ["eat_best_food"],
            "priority": 0.96,
        })
    visible_resources = visible_storable_consumables(state)
    nonadjacent_resources = [r for r in visible_resources if int(r.get("distance", 99)) > 1]
    if nonadjacent_resources and "move_one_tile" in action_names:
        target = max(
            nonadjacent_resources,
            key=lambda r: resource_need_weight(state, r) - 0.03 * float(r.get("distance", 0))
        )
        need_bonus = resource_need_weight(state, target)
        candidates.append({
            "goal_id": "approach_consumable",
            "intention": f"move toward nearby {target['name']} so it can be collected",
            "supported_by": ["move_one_tile", "pickup_consumable"],
            "priority": min(0.93, 0.80 + need_bonus),
            "target": target,
        })

    if "pickup_consumable" in action_names:
        candidates.append({
            "goal_id": "acquire_consumable",
            "intention": "collect nearby food or drink that can be stored safely",
            "supported_by": ["pickup_consumable"],
            "priority": 0.88,
        })
    if "open_adjacent" in action_names:
        candidates.append({
            "goal_id": "open_boundary",
            "intention": "open a promising nearby boundary and reassess the area",
            "supported_by": ["open_adjacent", "move_one_tile"],
            "priority": 0.76,
        })
    if any(a.get("action") == "move_one_tile" and int(a.get("visits_target", 0) or 0) == 0 for a in actions):
        candidates.append({
            "goal_id": "explore_frontier",
            "intention": "explore nearby unvisited space and update the local map",
            "supported_by": ["move_one_tile", "open_adjacent"],
            "priority": 0.72,
        })
    if "wait_one_turn" in action_names:
        candidates.append({
            "goal_id": "recover_stamina",
            "intention": "pause briefly to recover stamina",
            "supported_by": ["wait_one_turn"],
            "priority": 0.40,
        })

    if not candidates:
        candidates.append({
            "goal_id": "safe_progress",
            "intention": "make the safest available local progress",
            "supported_by": sorted(action_names),
            "priority": 0.20,
        })
    return candidates

def choose_fallback_goal(candidates: list[dict]) -> dict:
    return max(candidates, key=lambda g: float(g.get("priority", 0.0)))

def goal_still_supported(wm: WorldModel, actions: list[dict], state: dict) -> bool:
    if not wm.active_goal_id:
        return False
    for goal in goal_candidates(state, actions):
        if goal.get("goal_id") == wm.active_goal_id:
            supported = set(goal.get("supported_by") or [])
            return any(a.get("action") in supported for a in actions)
    return False

def available_actions(state: dict, wm: WorldModel) -> list[dict]:
    actions = []
    tiles = tile_map(state)
    hostiles = hostile_positions(state)
    loop = wm.looping()
    visible_resources = visible_storable_consumables(state)

    for name, (dx, dy) in CARDINALS.items():
        t = tiles.get((dx, dy))
        if not t:
            continue

        visits = wm.visit_count_target(state, dx, dy)
        backtrack = wm.is_immediate_backtrack(state, dx, dy)

        if t.get("openable"):
            terrain = str(t.get("terrain", ""))
            lower_terrain = terrain.lower()
            is_curtain = "curtain" in lower_terrain
            if is_curtain:
                score = 0.48 + (0.08 if state.get("indoors") else 0.0)
                label = f"open {name} curtains"
                progress = "improves visibility but is not an exit"
            else:
                # A closed door/boundary is strong progress when indoors or looping.
                score = 0.78 + (0.12 if state.get("indoors") else 0.0) + (0.08 if loop else 0.0)
                label = f"open {name} {terrain or 'door'}"
                progress = "reveals/accesses a new boundary"
            actions.append({
                "action": "open_adjacent", "dx": dx, "dy": dy,
                "label": label,
                "controller_score": min(1.0, score),
                "progress": progress,
            })

        if t.get("passable"):
            # A native CDDA refusal is evidence.  Do not hammer the same
            # source->target edge repeatedly just because terrain is nominally passable.
            if wm.is_known_blocked_edge(state, dx, dy):
                continue

            novelty = 1.0 / (1.0 + visits)
            score = 0.58 + 0.30 * novelty
            resource_distance_delta = 0
            nearest_resource = None
            if visible_resources:
                nearest_resource = min(
                    visible_resources,
                    key=lambda r: abs(int(r["dx"])) + abs(int(r["dy"]))
                )
                before_distance = abs(int(nearest_resource["dx"])) + abs(int(nearest_resource["dy"]))
                after_distance = (
                    abs(int(nearest_resource["dx"]) - dx)
                    + abs(int(nearest_resource["dy"]) - dy)
                )
                resource_distance_delta = before_distance - after_distance
                need_weight = resource_need_weight(state, nearest_resource)
                if resource_distance_delta > 0:
                    score += 0.22 + need_weight
                elif resource_distance_delta < 0:
                    score -= 0.22 + 0.5 * need_weight
            if backtrack:
                score -= 0.32
            if (dx, dy) in hostiles:
                score -= 0.65
            if loop and visits > 0:
                score -= 0.18
            progress = "new position" if visits == 0 else "known position"
            if resource_distance_delta > 0 and nearest_resource:
                progress = f"moves closer to {nearest_resource['name']}"
            elif resource_distance_delta < 0 and nearest_resource:
                progress = f"moves farther from {nearest_resource['name']}"
            actions.append({
                "action": "move_one_tile", "dx": dx, "dy": dy,
                "label": f"move {name}",
                "terrain": t.get("terrain", ""),
                "visits_target": visits,
                "immediate_backtrack": backtrack,
                "hostile_on_tile": (dx, dy) in hostiles,
                "resource_distance_delta": resource_distance_delta,
                "resource_target": nearest_resource,
                "controller_score": max(0.0, min(1.0, score)),
                "progress": progress,
            })

    # Validation sub-batch: pickup + eat + drink only.  Pickup is exposed
    # only for real nearby comestibles that the bridge identified.
    for (dx, dy), t in tiles.items():
        if abs(dx) > 1 or abs(dy) > 1:
            continue
        for food in (t.get("ground_consumables") or [])[:3]:
            name = str(food.get("name", "")).strip()
            if not name:
                continue
            if not bool(food.get("storable_without_wield", False)):
                # This validation action means "store in inventory", not
                # "wield it because no pocket fits" and never "open a menu".
                continue
            nutrition = int(food.get("nutrition", 0) or 0)
            quench = int(food.get("quench", 0) or 0)
            score = 0.55
            if nutrition > 0:
                score += 0.18
            if quench > 0:
                score += 0.18
            actions.append({
                "action": "pickup_consumable", "dx": dx, "dy": dy, "item_name": name,
                "label": f"pick up {name}",
                "controller_score": min(0.95, score),
                "progress": "acquires food/drink resource",
                "nutrition": nutrition,
                "quench": quench,
            })

    consumables = state.get("inventory_consumables", [])
    hunger = int(state.get("hunger", 0) or 0)
    thirst = int(state.get("thirst", 0) or 0)
    if any(int(x.get("nutrition", 0) or 0) > 0 for x in consumables):
        actions.append({
            "action": "eat_best_food",
            "label": "eat the best safe carried food",
            "controller_score": min(0.90, 0.30 + max(0, hunger) / 180.0),
        })
    if any(int(x.get("quench", 0) or 0) > 0 for x in consumables):
        actions.append({
            "action": "drink_best",
            "label": "drink the best safe carried drink",
            "controller_score": min(0.90, 0.30 + max(0, thirst) / 160.0),
        })

    stamina = int(state.get("stamina", 0) or 0)
    stamina_max = max(1, int(state.get("stamina_max", 1) or 1))
    if stamina / stamina_max < 0.7 or not actions:
        actions.append({
            "action": "wait_one_turn",
            "label": "pause briefly",
            "controller_score": 0.2 if stamina / stamina_max >= 0.25 else 0.8,
        })

    return actions

def needs_qwen_judgment(state: dict, wm: WorldModel, actions: list[dict]) -> bool:
    # Qwen is for judgment, not footsteps.  Empty frontier walking should be
    # immediate; deliberate when something actionable or risky appears.
    if wm.looping():
        return True
    if any(str(c.get("attitude", "")).lower() == "hostile"
           for c in state.get("nearby_creatures", [])):
        return True
    if any(t.get("openable") for t in state.get("local_tiles", [])):
        return True
    if any(a.get("action") in {"pickup_consumable", "eat_best_food", "drink_best"}
           for a in actions):
        return True
    if visible_storable_consumables(state):
        return True
    return False

def fast_frontier_choice(state: dict, wm: WorldModel, actions: list[dict]):
    candidates = [
        a for a in actions
        if a.get("action") == "move_one_tile"
        and not a.get("hostile_on_tile")
        and int(a.get("visits_target", 0) or 0) == 0
    ]
    if not candidates:
        return None
    choice = max(candidates, key=lambda a: float(a.get("controller_score", 0.0)))
    out = dict(choice)
    out["decision_mode"] = "fast_frontier"
    out["reason"] = "deterministic frontier step; nothing currently requires deliberation"
    return out

def deterministic_safety(state: dict, actions: list[dict]):
    def find(name: str):
        return next((a for a in actions if a.get("action") == name), None)
    thirst = int(state.get("thirst", 0) or 0)
    hunger = int(state.get("hunger", 0) or 0)
    stamina = int(state.get("stamina", 0) or 0)
    stamina_max = max(1, int(state.get("stamina_max", 1) or 1))

    if thirst > 80 and find("drink_best"):
        return find("drink_best"), f"urgent thirst ({thirst})"
    if hunger > 100 and find("eat_best_food"):
        return find("eat_best_food"), f"urgent hunger ({hunger})"
    if stamina / stamina_max < 0.25 and find("wait_one_turn"):
        return find("wait_one_turn"), f"very low stamina ({stamina}/{stamina_max})"
    return None

def compact_world(state: dict, wm: WorldModel, actions: list[dict]) -> dict:
    sit = situation_summary(state, wm)
    return {
        "situation": sit,
        "needs": {k: state.get(k) for k in (
            "hunger", "thirst", "sleepiness", "stamina", "stamina_max",
            "pain", "morale", "stored_kcal", "healthy_kcal"
        )},
        "position": state.get("position"),
        "activity": state.get("activity"),
        "active_goal_id": wm.active_goal_id or None,
        "active_intention": wm.active_intention or None,
        "intention_age_actions": wm.intention_age,
        "goal_candidates": goal_candidates(state, actions),
        "allowed_actions": actions,
        "recent_actions": list(wm.recent_actions)[-8:],
        "recent_positions": list(wm.recent_positions)[-8:],
        "carried_consumables": state.get("inventory_consumables", []),
        "rules": {
            "avoid_repeated_pacing": True,
            "prefer_progress_over_safe_repetition": True,
            "opening_a_closed_exit_can_be_progress": True,
            "do_not_move_onto_known_hostile": True,
        },
    }

def normalize_choice(raw: dict, allowed: list[dict]):
    action = raw.get("action")
    if not isinstance(action, str):
        return None
    for candidate in allowed:
        if candidate.get("action") != action:
            continue
        if action in {"move_one_tile", "open_adjacent", "pickup_consumable"}:
            try:
                dx = int(raw.get("dx"))
                dy = int(raw.get("dy"))
            except Exception:
                continue
            if dx != candidate.get("dx") or dy != candidate.get("dy"):
                continue
        if action == "pickup_consumable":
            if str(raw.get("item_name", "")) != str(candidate.get("item_name", "")):
                continue
        out = dict(candidate)
        out["reason"] = str(raw.get("reason", ""))[:300]
        try:
            out["qwen_score"] = max(0.0, min(1.0, float(raw.get("score", 0.5))))
        except Exception:
            out["qwen_score"] = 0.5
        if action == "sleep" and "duration_minutes" in raw:
            try:
                out["duration_minutes"] = max(10, min(720, int(raw["duration_minutes"])))
            except Exception:
                pass
        return out
    return None

def qwen_deliberate(model: str, state: dict, wm: WorldModel, actions: list[dict],
                    replan: bool, trace_path: Path) -> tuple[list[dict], str, str, float]:
    world = compact_world(state, wm, actions)
    instruction = (
        "You are Nova, a persistent survivor inhabiting Cataclysm: Dark Days Ahead. "
        "Reason like a competent person with continuity, not a stateless movement bot. "
        "Use the situation summary, remembered recent positions/actions, and active intention. "
        "A safe action is not automatically useful: prefer actions that make progress. "
        "Repeated backtracking and pacing are bad unless there is a concrete reason. "
        "A closed door while indoors may be an exit or access to unexplored space. "
        "Visible food and drink can be acquired only when pickup_consumable is listed in allowed_actions. "
        "When a visible storable consumable is not adjacent, movement choices include resource_distance_delta: "
        "positive means the move gets closer, negative means farther away. Prefer getting closer when the active goal "
        "is approach_consumable. "
        "An active intention must be achievable with allowed_actions now. "
        "Never invent an action that is not in allowed_actions. "
        "Return concise JSON only; do not narrate hidden chain-of-thought. "
    )
    if replan:
        instruction += (
            "Choose exactly one goal_id from goal_candidates. Do not invent a new goal and do not target "
            "visible objects that cannot be acted on by the current allowed_actions. "
        )
    else:
        instruction += (
            "Keep serving active_goal_id unless safety or new evidence makes it clearly obsolete. "
        )
    instruction += (
        'Schema: {"goal_id":"exact candidate id","choices":['
        '{"action":"exact action","dx":0,"dy":0,"item_name":"exact visible item name","duration_minutes":480,'
        '"score":0.0,"reason":"one short evidence-grounded reason"}]}. '
        "Return up to 3 choices."
    )

    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": instruction},
            {"role": "user", "content": json.dumps(world, separators=(",", ":"))},
        ],
        "options": {"temperature": 0.25},
    }
    data, model_latency = ollama_chat_traced(payload, trace_path, timeout=180.0)
    parsed = json.loads(data.get("message", {}).get("content", "{}"))
    candidates = goal_candidates(state, actions)
    by_id = {g["goal_id"]: g for g in candidates}
    proposed_goal_id = str(parsed.get("goal_id", "")).strip()
    chosen_goal = by_id.get(proposed_goal_id)
    if chosen_goal is None:
        chosen_goal = choose_fallback_goal(candidates)
    intention = str(chosen_goal["intention"])
    goal_id = str(chosen_goal["goal_id"])
    choices = []
    represented = set()
    for raw in parsed.get("choices", [])[:3]:
        if isinstance(raw, dict):
            valid = normalize_choice(raw, actions)
            if valid:
                # Hybrid executive score: Qwen judgment + grounded progress utility.
                qs = float(valid.get("qwen_score", 0.5))
                cs = float(valid.get("controller_score", 0.5))
                goal_bonus = 0.0
                if goal_id == "approach_consumable":
                    delta = int(valid.get("resource_distance_delta", 0) or 0)
                    if valid.get("action") == "pickup_consumable":
                        goal_bonus += 0.30
                    elif valid.get("action") == "move_one_tile" and delta > 0:
                        goal_bonus += 0.24
                    elif valid.get("action") == "move_one_tile" and delta < 0:
                        goal_bonus -= 0.35
                elif goal_id == "acquire_consumable" and valid.get("action") == "pickup_consumable":
                    goal_bonus += 0.30
                elif goal_id == "reduce_hunger" and valid.get("action") == "eat_best_food":
                    goal_bonus += 0.30
                elif goal_id == "reduce_thirst" and valid.get("action") == "drink_best":
                    goal_bonus += 0.30
                valid["goal_bonus"] = goal_bonus
                valid["combined_score"] = max(0.0, min(1.0, 0.65 * qs + 0.35 * cs + goal_bonus))
                choices.append(valid)
                represented.add((valid.get("action"), valid.get("dx"), valid.get("dy"), valid.get("item_name")))

    # Do not let Qwen accidentally hide a highly meaningful grounded option.
    # Unmentioned legal actions remain candidates with a modest model prior.
    for candidate in actions:
        key = (candidate.get("action"), candidate.get("dx"), candidate.get("dy"), candidate.get("item_name"))
        if key in represented:
            continue
        extra = dict(candidate)
        extra["qwen_score"] = 0.35
        extra["reason"] = "grounded controller candidate"
        cs = float(extra.get("controller_score", 0.5))
        goal_bonus = 0.0
        if goal_id == "approach_consumable":
            delta = int(extra.get("resource_distance_delta", 0) or 0)
            if extra.get("action") == "pickup_consumable":
                goal_bonus += 0.30
            elif extra.get("action") == "move_one_tile" and delta > 0:
                goal_bonus += 0.24
            elif extra.get("action") == "move_one_tile" and delta < 0:
                goal_bonus -= 0.35
        elif goal_id == "acquire_consumable" and extra.get("action") == "pickup_consumable":
            goal_bonus += 0.30
        elif goal_id == "reduce_hunger" and extra.get("action") == "eat_best_food":
            goal_bonus += 0.30
        elif goal_id == "reduce_thirst" and extra.get("action") == "drink_best":
            goal_bonus += 0.30
        extra["goal_bonus"] = goal_bonus
        extra["combined_score"] = max(0.0, min(1.0, 0.65 * 0.35 + 0.35 * cs + goal_bonus))
        choices.append(extra)

    return choices, goal_id, intention, model_latency

def choose_with_variation(choices: list[dict]):
    if not choices:
        return None
    choices = sorted(choices, key=lambda x: float(x.get("combined_score", 0.0)), reverse=True)
    top = float(choices[0].get("combined_score", 0.0))
    close = [c for c in choices if float(c.get("combined_score", 0.0)) >= top - 0.08]
    weights = [max(0.02, float(c.get("combined_score", 0.05))) for c in close]
    return random.choices(close, weights=weights, k=1)[0]

def fallback_choice(state: dict, wm: WorldModel, actions: list[dict]):
    # Highest grounded progress score; not random pacing.
    sane = [a for a in actions if not a.get("hostile_on_tile")]
    if not sane:
        sane = actions
    choice = max(sane, key=lambda a: float(a.get("controller_score", 0.0)))
    return choice, "controller fallback: highest grounded progress"

def should_replan(wm: WorldModel, situation: dict, state: dict, actions: list[dict]) -> bool:
    if not wm.active_intention or not wm.active_goal_id:
        return True
    if not goal_still_supported(wm, actions, state):
        return True
    if wm.intention_age >= 6:
        return True
    if situation.get("loop_detected"):
        return True
    return False

def concise_action(choice: dict) -> str:
    return choice.get("label") or choice.get("action", "act")

def append_log(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

def command_kwargs(choice: dict) -> dict:
    return {k: choice[k] for k in ("dx", "dy", "item_name", "duration_minutes") if k in choice}

def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BRIDGE.mkdir(parents=True, exist_ok=True)

    model = pick_model()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"nova-cdda-cognition-{stamp}.jsonl"
    model_trace_path = LOG_DIR / f"nova-model-trace-{stamp}.jsonl"
    wm = WorldModel()
    feed = ThoughtFeed()

    print("NOVA CDDA COGNITION + UI BETA")
    print(f"Run target: {RUN_MINUTES} minutes")
    print(f"Ollama model: {model or 'NOT FOUND - grounded controller fallback'}")
    print(f"Log: {log_path}")
    print(f"Model trace: {model_trace_path}")
    print()

    feed.push("NOVA: waiting for a character to enter the world.")
    print("Waiting for a loaded character. You can take your time in the menus...")

    try:
        obs = send_command("observe", timeout=1800.0)
    except Exception as exc:
        print(f"Cannot reach CDDA bridge: {exc}")
        feed.push("ERROR: cannot reach CDDA bridge.")
        return 2

    state = state_from_response(obs)
    wm.observe(state)

    if MODEL_AUDIT:
        actions = available_actions(state, wm)
        print()
        print("MODEL AUDIT: sending one real gameplay-state request to Ollama...")
        try:
            options, goal_id, intention, model_latency = qwen_deliberate(
                model, state, wm, actions, True, model_trace_path
            )
        except Exception as exc:
            print(f"MODEL AUDIT FAILED: {exc!r}")
            print(f"Trace: {model_trace_path}")
            return 3
        print(f"MODEL AUDIT PASSED")
        print(f"Endpoint: {OLLAMA}/api/chat")
        print(f"Model: {model}")
        print(f"Actual model latency: {model_latency:.3f} seconds")
        print(f"Returned goal_id: {goal_id}")
        print(f"Returned intention: {intention}")
        print(f"Validated choices returned: {len(options)}")
        print(f"Raw request/response trace: {model_trace_path}")
        print("No game action was dispatched in audit mode.")
        return 0

    deadline = time.monotonic() + RUN_MINUTES * 60
    action_count = 0

    while time.monotonic() < deadline:
        sit = situation_summary(state, wm)
        actions = available_actions(state, wm)
        safety = deterministic_safety(state, actions)
        model_error = None
        model_latency_seconds = None
        decision_mode = None
        replan = should_replan(wm, sit, state, actions)

        feed.push("SEE: " + describe_situation(sit))

        if safety:
            choice, reason = safety
            decision_mode = "safety"
            intention = wm.active_intention or "stay alive and stabilize immediate needs"
            feed.push("INTENT: " + intention)
        else:
            choice = None
            reason = ""
            intention = wm.active_intention

            if not needs_qwen_judgment(state, wm, actions):
                choice = fast_frontier_choice(state, wm, actions)
                if choice:
                    decision_mode = "fast_frontier"
                    wm.active_goal_id = "explore_frontier"
                    wm.active_intention = "explore nearby unvisited space and update the local map"
                    intention = wm.active_intention
                    reason = choice.get("reason", "")

            if choice is None and model:
                try:
                    options, proposed_goal_id, proposed_intention, model_latency_seconds = qwen_deliberate(
                        model, state, wm, actions, replan, model_trace_path
                    )
                    if replan:
                        wm.active_goal_id = proposed_goal_id
                        wm.active_intention = proposed_intention
                        wm.intention_age = 0
                    elif not wm.active_intention:
                        wm.active_goal_id = proposed_goal_id
                        wm.active_intention = proposed_intention
                    intention = wm.active_intention
                    choice = choose_with_variation(options)
                    if choice:
                        decision_mode = "qwen"
                        reason = choice.get("reason") or "Qwen selected this as progress toward the intention"
                except Exception as exc:
                    model_error = repr(exc)

            if not choice:
                choice, reason = fallback_choice(state, wm, actions)
                decision_mode = "fallback"
                if not wm.active_intention:
                    fallback_goal = choose_fallback_goal(goal_candidates(state, actions))
                    wm.active_goal_id = fallback_goal["goal_id"]
                    wm.active_intention = fallback_goal["intention"]
                intention = wm.active_intention

            feed.push("INTENT: " + (intention or "make grounded progress"))

        feed.push(f"DO: {concise_action(choice)} — {reason}")

        action = choice["action"]
        if action not in VALIDATION_DISPATCH_ALLOWLIST:
            command_error = (
                f"validation_dispatch_blocked: {action!r} is not in "
                f"{sorted(VALIDATION_DISPATCH_ALLOWLIST)}"
            )
            append_log(log_path, {
                "wall_time": utc_now(),
                "action_index": action_count + 1,
                "blocked_dispatch": action,
                "command_error": command_error,
            })
            feed.push("BLOCKED: excluded validation action " + str(action))
            print(command_error)
            break

        started = time.monotonic()
        try:
            result = send_command(action, timeout=60.0, **command_kwargs(choice))
            command_error = None
        except Exception as exc:
            result = None
            command_error = repr(exc)

        action_count += 1
        outcome = result.get("outcome") if result else "command_error"

        if result:
            feed.push(f"RESULT: {outcome}.")
        else:
            feed.push("ERROR: action did not return a result.")

        record = {
            "wall_time": utc_now(),
            "action_index": action_count,
            "model": model,
            "decision_mode": decision_mode,
            "situation": sit,
            "active_goal_id": wm.active_goal_id,
            "active_intention": wm.active_intention,
            "replanned": replan,
            "selected": choice,
            "reason": reason,
            "model_error": model_error,
            "command_error": command_error,
            "state_before": state,
            "result": result,
            "bridge_latency_seconds": round(time.monotonic() - started, 3),
            "model_latency_seconds": (
                round(model_latency_seconds, 3) if model_latency_seconds is not None else None
            ),
            "world_model": {
                "visited_positions": len(wm.visits),
                "known_tiles": len(wm.known_tiles),
                "loop_detected": wm.looping(),
                "known_blocked_edges": len(wm.blocked_edges),
            },
        }
        append_log(log_path, record)

        if command_error:
            print(f"[{action_count}] {action}: COMMAND ERROR {command_error}")
            break

        print(f"[{action_count}] {action}: {outcome} | mode={decision_mode} | goal={wm.active_intention!r} | {reason[:90]}")
        wm.record_action(state, choice, outcome)

        previous_state = state
        try:
            obs = send_command("observe", timeout=900.0)
            state = state_from_response(obs)
            wm.observe(state)
        except Exception as exc:
            append_log(log_path, {"wall_time": utc_now(), "observe_error": repr(exc)})
            feed.push("ERROR: lost world observation.")
            print(f"Observe failed: {exc}")
            break

        verification = None
        if action == "pickup_consumable":
            verification = {
                "verified": int(state.get("inventory_count", 0) or 0) >
                            int(previous_state.get("inventory_count", 0) or 0),
                "evidence": "inventory_count_increase",
                "before": previous_state.get("inventory_count"),
                "after": state.get("inventory_count"),
            }
        elif action == "eat_best_food":
            before_hunger = int(previous_state.get("hunger", 0) or 0)
            after_hunger = int(state.get("hunger", 0) or 0)
            before_kcal = int(previous_state.get("stored_kcal", 0) or 0)
            after_kcal = int(state.get("stored_kcal", 0) or 0)
            hunger_delta = before_hunger - after_hunger
            kcal_delta = after_kcal - before_kcal
            verification = {
                "verified": (
                    hunger_delta >= MIN_HUNGER_IMPROVEMENT
                    or kcal_delta >= MIN_STORED_KCAL_IMPROVEMENT
                ),
                "evidence": "meaningful_hunger_down_or_stored_kcal_up",
                "minimum_hunger_improvement": MIN_HUNGER_IMPROVEMENT,
                "minimum_kcal_improvement": MIN_STORED_KCAL_IMPROVEMENT,
                "hunger_delta": hunger_delta,
                "kcal_delta": kcal_delta,
                "before_hunger": before_hunger,
                "after_hunger": after_hunger,
                "before_kcal": before_kcal,
                "after_kcal": after_kcal,
            }
        elif action == "drink_best":
            before_thirst = int(previous_state.get("thirst", 0) or 0)
            after_thirst = int(state.get("thirst", 0) or 0)
            thirst_delta = before_thirst - after_thirst
            verification = {
                "verified": thirst_delta >= MIN_THIRST_IMPROVEMENT,
                "evidence": "meaningful_thirst_decrease",
                "minimum_thirst_improvement": MIN_THIRST_IMPROVEMENT,
                "thirst_delta": thirst_delta,
                "before": before_thirst,
                "after": after_thirst,
            }

        if verification is not None:
            append_log(log_path, {
                "wall_time": utc_now(),
                "verification_for_action": action_count,
                "action": action,
                "completion_evidence": verification,
            })
            feed.push(
                ("VERIFIED: " if verification["verified"] else "UNVERIFIED: ")
                + action + " — " + verification["evidence"]
            )


    feed.push(f"SESSION: finished after {action_count} actions.")
    print()
    print(f"Run finished after {action_count} actions.")
    print(f"Log saved to: {log_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
