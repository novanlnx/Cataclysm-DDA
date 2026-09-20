from __future__ import annotations

import hashlib
import json
import os
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

EVOLUTION_STATE_VERSION = 1
LIFE_RECORD_SCHEMA_VERSION = 1
LIFE_STATUS_SCHEMA_VERSION = 1
LESSON_SCHEMA_VERSION = 1
LIFE_STATUS_PATH = BRIDGE / "life-status.json"
EVOLUTION_STATE_PATH = STATE_DIR / "nova-evolution-state-v1.json"
LIFE_HISTORY_PATH = STATE_DIR / "nova-life-history-v1.jsonl"
LESSON_PATH = STATE_DIR / "nova-lessons-v1.jsonl"
ACTIVE_LIFE_PATH = STATE_DIR / "nova-active-life-v1.json"
CONSUMED_LIFE_STATUS_PATH = STATE_DIR / "nova-consumed-life-status-v1.json"

CARDINALS = {
    "north": (0, -1),
    "south": (0, 1),
    "west": (-1, 0),
    "east": (1, 0),
}

MOVE_DIRECTIONS = {
    "north": (0, -1),
    "northeast": (1, -1),
    "east": (1, 0),
    "southeast": (1, 1),
    "south": (0, 1),
    "southwest": (-1, 1),
    "west": (-1, 0),
    "northwest": (-1, -1),
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

# Validation gating: do not expose a homeostatic action when the measured
# need is already satisfied.  These are intentionally conservative until
# real CDDA before/after evidence justifies richer appetite logic.
EAT_NEED_HUNGER = 20
DRINK_NEED_THIRST = 20

THREAT_RANGE_TILES = 3
CRITICAL_NEED_THRESHOLD = 80
STAMINA_LOW_RATIO = 0.35
LESSON_MATCH_THRESHOLD = 3
LESSON_ACTION_BIAS = 0.35
FAST_FRONTIER_MOVES = 64
SHORELINE_HISTORY = 32
SHORELINE_TRIGGER_SAMPLES = 12
SHORELINE_NEAR_DISTANCE = 2
SHORELINE_ESCAPE_DISTANCE = 7
SHORELINE_CLEAR_SAMPLES = 6
SURVIVAL_MISSION = (
    "Stay alive and improve survival prospects: avoid lethal hazards, "
    "meet critical needs, secure usable shelter, acquire food/water, "
    "and explore only when it advances those objectives."
)
STRATEGIC_TRAJECTORY_HISTORY = 96
STRATEGIC_STALL_WINDOW = 64
STRATEGIC_STALL_MIN_PATH = 40
STRATEGIC_STALL_MAX_NET = 14
STRATEGIC_STALL_MAX_EFFICIENCY = 0.28
STRATEGIC_STALL_CLEAR_DISTANCE = 18

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

class LifeEnded(RuntimeError):
    def __init__(self, status: dict):
        super().__init__("Nova life ended")
        self.status = status

class BridgeTransportError(RuntimeError):
    pass

def write_command_atomic(path: Path, payload: dict) -> None:
    """Publish command.json as verified UTF-8 bytes with no BOM."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if not raw.startswith(b"{"):
        raise BridgeTransportError("Refusing to publish malformed command payload")
    last_error = None
    for _ in range(40):
        try:
            with tmp.open("wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            if tmp.read_bytes() != raw:
                raise BridgeTransportError("Command temp-file verification failed")
            tmp.replace(path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.025)
    if last_error:
        raise BridgeTransportError(f"Could not publish command.json: {last_error}")

def read_json_safe(path: Path) -> dict | None:
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None

def read_bridge_json_tolerant(path: Path) -> dict | None:
    """Read a bridge response without letting one bad diagnostic byte hide the protocol event."""
    try:
        if not path.exists():
            return None
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None

def read_life_status() -> dict | None:
    status = read_json_safe(LIFE_STATUS_PATH)
    if not status:
        return None
    if int(status.get("schema_version", -1)) != LIFE_STATUS_SCHEMA_VERSION:
        return None
    return status

def life_status_signature(status: dict | None) -> str | None:
    if not status or str(status.get("status", "")) != "dead":
        return None
    payload = {
        "schema_version": status.get("schema_version"),
        "turn": status.get("turn"),
        "position": status.get("position"),
        "status": status.get("status"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

def archive_consumed_life_status(status: dict | None) -> None:
    if status:
        write_json_atomic(CONSUMED_LIFE_STATUS_PATH, {
            "schema_version": LIFE_STATUS_SCHEMA_VERSION,
            "consumed_at": utc_now(),
            "status": status,
        })
    try:
        LIFE_STATUS_PATH.unlink()
    except OSError:
        pass

def load_evolution_state() -> dict:
    state = read_json_safe(EVOLUTION_STATE_PATH) or {}
    if int(state.get("version", -1)) != EVOLUTION_STATE_VERSION:
        return {
            "version": EVOLUTION_STATE_VERSION,
            "next_life_number": 1,
            "life_number": 1,
            "lives_completed": 0,
        }
    if "next_life_number" not in state:
        state["next_life_number"] = int(state.get("life_number", 1) or 1)
    state["life_number"] = int(state.get("next_life_number", 1) or 1)
    state.setdefault("lives_completed", 0)
    return state

def save_evolution_state(state: dict) -> None:
    payload = dict(state)
    payload["version"] = EVOLUTION_STATE_VERSION
    write_json_atomic(EVOLUTION_STATE_PATH, payload)

def append_life_history(record: dict) -> None:
    LIFE_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(record)
    payload["schema_version"] = LIFE_RECORD_SCHEMA_VERSION
    with LIFE_HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

def load_recent_life_history(limit: int = 5, include_aborted: bool = False) -> list[dict]:
    if not LIFE_HISTORY_PATH.exists():
        return []
    try:
        lines = LIFE_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        if int(item.get("schema_version", -1)) != LIFE_RECORD_SCHEMA_VERSION:
            continue
        if not include_aborted and item.get("terminal_state") != "dead":
            continue
        out.append(item)
        if len(out) >= max(1, limit):
            break
    out.reverse()
    return out

def previous_life_context(limit: int = 5) -> list[dict]:
    summaries = []
    for record in load_recent_life_history(limit=limit, include_aborted=False):
        summaries.append({
            "life_id": record.get("life_id"),
            "life_number": record.get("life_number"),
            "terminal_state": record.get("terminal_state"),
            "ended_at": record.get("ended_at"),
            "duration_seconds": record.get("duration_seconds"),
            "duration_game_turns": record.get("duration_game_turns"),
        })
    return summaries

def send_command(action: str, timeout: float = 180.0, **kwargs) -> dict:
    BRIDGE.mkdir(parents=True, exist_ok=True)
    command_path = BRIDGE / "command.json"
    unknown_response_path = BRIDGE / "response-unknown.json"
    payload_base = {"action": action, **kwargs}
    last_invalid_error = None

    # A malformed command is known not to have executed because the bridge
    # rejects it before dispatch. One retry is therefore safe and prevents a
    # transient command-file corruption from turning into a 60-second timeout.
    for attempt in range(2):
        command_id = uuid.uuid4().hex
        response_path = BRIDGE / f"response-{command_id}.json"
        payload = {"id": command_id, **payload_base}

        for stale in (command_path, response_path, unknown_response_path):
            try:
                stale.unlink()
            except OSError:
                pass

        write_command_atomic(command_path, payload)
        deadline = time.monotonic() + timeout

        while deadline is None or time.monotonic() < deadline:
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

            if unknown_response_path.exists():
                unknown = read_bridge_json_tolerant(unknown_response_path)
                if unknown and unknown.get("outcome") == "invalid_command":
                    last_invalid_error = str(unknown.get("error") or "invalid_command")
                    try:
                        unknown_response_path.unlink()
                    except OSError:
                        pass
                    break

            life_status = read_life_status()
            if life_status and str(life_status.get("status", "")) == "dead":
                raise LifeEnded(life_status)
            time.sleep(0.05)
        else:
            life_status = read_life_status()
            if life_status and str(life_status.get("status", "")) == "dead":
                raise LifeEnded(life_status)
            alive_note = (
                " while bridge life-status still reported alive"
                if life_status and str(life_status.get("status", "")) == "alive"
                else ""
            )
            raise BridgeTransportError(
                f"Timed out waiting for CDDA response to {action} ({command_id}){alive_note}"
            )

        if attempt == 0:
            continue

    raise BridgeTransportError(
        f"Bridge rejected {action} command as invalid after retry: {last_invalid_error}"
    )

def ollama_json(path: str, payload: dict | None = None, timeout: float = 180.0) -> dict:
    url = OLLAMA + path
    if payload is None:
        req = request.Request(url, method="GET")
    else:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def ollama_chat_traced(payload: dict, trace_path: Path, timeout: float = 180.0) -> tuple[dict, float, dict]:
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
            data = json.loads(response_body)
            ollama_metrics = {
                "total_duration_seconds": round(float(data.get("total_duration", 0) or 0) / 1_000_000_000.0, 3),
                "load_duration_seconds": round(float(data.get("load_duration", 0) or 0) / 1_000_000_000.0, 3),
                "prompt_eval_duration_seconds": round(float(data.get("prompt_eval_duration", 0) or 0) / 1_000_000_000.0, 3),
                "eval_duration_seconds": round(float(data.get("eval_duration", 0) or 0) / 1_000_000_000.0, 3),
                "prompt_eval_count": data.get("prompt_eval_count"),
                "eval_count": data.get("eval_count"),
            }
            append_log(trace_path, {
                "wall_time": utc_now(),
                "endpoint": url,
                "http_method": "POST",
                "request_body": request_body,
                "response_status": status,
                "response_body": response_body,
                "model_latency_seconds": round(elapsed, 3),
                "ollama_metrics": ollama_metrics,
            })
            return data, elapsed, ollama_metrics
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
    hazard_tiles: set[tuple[int, int, int]] = field(default_factory=set)
    stall_avoid_tiles: set[tuple[int, int, int]] = field(default_factory=set)
    known_landmarks: dict[tuple[int, int, int], dict] = field(default_factory=dict)
    failed_landmarks: set[tuple[int, int, int]] = field(default_factory=set)
    shelter_anchor: tuple[int, int, int] | None = None
    last_planner_assessment: str = ""
    last_planner_blocker: str = ""
    last_planner_next_step: str = ""
    last_action_summary: str = ""
    active_goal_id: str = ""
    active_intention: str = ""
    priority_context: dict = field(default_factory=dict)
    last_lesson_signature: str = ""
    progress_epoch: int = 0
    no_progress_streak: int = 0
    goal_no_progress: dict[str, int] = field(default_factory=dict)
    recent_water_distance: deque = field(default_factory=lambda: deque(maxlen=SHORELINE_HISTORY))
    shoreline_escape_active: bool = False
    shoreline_clear_streak: int = 0
    trajectory_positions: deque = field(default_factory=lambda: deque(maxlen=STRATEGIC_TRAJECTORY_HISTORY))
    strategic_stall_active: bool = False
    strategic_stall_anchor: tuple[int, int, int] | None = None

    def observe(self, state: dict) -> None:
        p = pos_tuple(state)
        self.visits[p] = self.visits.get(p, 0) + 1
        self.recent_positions.append(p)
        self.trajectory_positions.append(p)
        px, py, pz = p
        for t in state.get("local_tiles", []):
            try:
                gx = px + int(t["dx"])
                gy = py + int(t["dy"])
                tile_key = (gx, gy, pz)
                self.known_tiles[tile_key] = {
                    "terrain": t.get("terrain", ""),
                    "passable": bool(t.get("passable")),
                    "openable": bool(t.get("openable")),
                    "indoors": bool(t.get("indoors")),
                    "swimmable": bool(t.get("swimmable")),
                    "deep_water": bool(t.get("deep_water")),
                    "dangerous": bool(t.get("dangerous")),
                    "movement_hazard": bool(t.get("movement_hazard")),
                    "special_movement": bool(t.get("special_movement")),
                    "move_cost": int(t.get("move_cost", 0) or 0),
                    "items": list(t.get("items") or []),
                    "ground_consumables": [
                        dict(x) for x in (t.get("ground_consumables") or [])
                        if isinstance(x, dict)
                    ],
                }
                if bool(t.get("movement_hazard")) or bool(t.get("dangerous")) or bool(t.get("deep_water")):
                    self.hazard_tiles.add(tile_key)
            except Exception:
                pass
        for landmark in state.get("strategic_landmarks", []):
            try:
                gx = px + int(landmark.get("dx", 0) or 0)
                gy = py + int(landmark.get("dy", 0) or 0)
                key = (gx, gy, pz)
                self.known_landmarks[key] = {
                    "kind": str(landmark.get("kind", "landmark")),
                    "terrain": str(landmark.get("terrain", "")),
                    "first_seen_turn": int(state.get("turn", 0) or 0),
                }
            except Exception:
                pass

        if bool(state.get("indoors")):
            hostile_close = any(
                str(critter.get("attitude", "")).lower() == "hostile"
                and max(abs(int(critter.get("dx", 99) or 99)), abs(int(critter.get("dy", 99) or 99))) <= THREAT_RANGE_TILES
                for critter in state.get("nearby_creatures", [])
            )
            if not hostile_close and self.shelter_anchor is None:
                self.shelter_anchor = p

        water_distance = self.nearest_known_water_distance(p)
        if water_distance is not None:
            self.recent_water_distance.append(water_distance)
            recent = list(self.recent_water_distance)
            near_count = sum(1 for d in recent[-SHORELINE_TRIGGER_SAMPLES:] if d <= SHORELINE_NEAR_DISTANCE)
            if (
                not self.shoreline_escape_active
                and len(recent) >= SHORELINE_TRIGGER_SAMPLES
                and near_count >= SHORELINE_TRIGGER_SAMPLES - 2
            ):
                self.shoreline_escape_active = True
                self.shoreline_clear_streak = 0

            if self.shoreline_escape_active:
                if water_distance >= SHORELINE_ESCAPE_DISTANCE:
                    self.shoreline_clear_streak += 1
                else:
                    self.shoreline_clear_streak = 0
                if self.shoreline_clear_streak >= SHORELINE_CLEAR_SAMPLES:
                    self.shoreline_escape_active = False
                    self.shoreline_clear_streak = 0
                    self.recent_water_distance.clear()

        # Strategic stall detection is intentionally different from the local
        # blocked-edge/no-progress detector. Successful footsteps can still be
        # strategically useless if a long path folds back onto itself.
        trajectory = list(self.trajectory_positions)
        if not self.strategic_stall_active and len(trajectory) >= STRATEGIC_STALL_WINDOW:
            window = trajectory[-STRATEGIC_STALL_WINDOW:]
            path_distance = 0
            for a, b in zip(window, window[1:]):
                if a[2] != b[2]:
                    continue
                path_distance += max(abs(b[0] - a[0]), abs(b[1] - a[1]))
            start = window[0]
            end = window[-1]
            net_distance = (
                max(abs(end[0] - start[0]), abs(end[1] - start[1]))
                if end[2] == start[2] else path_distance
            )
            efficiency = (net_distance / path_distance) if path_distance > 0 else 1.0
            if (
                path_distance >= STRATEGIC_STALL_MIN_PATH
                and (
                    net_distance <= STRATEGIC_STALL_MAX_NET
                    or efficiency <= STRATEGIC_STALL_MAX_EFFICIENCY
                )
            ):
                self.strategic_stall_active = True
                self.strategic_stall_anchor = p
                self.trajectory_positions.clear()
                self.trajectory_positions.append(p)

        if self.strategic_stall_active and self.strategic_stall_anchor is not None:
            ax, ay, az = self.strategic_stall_anchor
            if pz == az and max(abs(px - ax), abs(py - ay)) >= STRATEGIC_STALL_CLEAR_DISTANCE:
                self.strategic_stall_active = False
                self.strategic_stall_anchor = None
                self.trajectory_positions.clear()
                self.trajectory_positions.append(p)

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

    def nearest_known_water_distance(self, point: tuple[int, int, int]) -> int | None:
        x, y, z = point
        distances = [
            max(abs(gx - x), abs(gy - y))
            for (gx, gy, gz), tile in self.known_tiles.items()
            if gz == z and (bool(tile.get("swimmable")) or bool(tile.get("deep_water")))
        ]
        return min(distances) if distances else None

    def target_water_distance(self, state: dict, dx: int, dy: int) -> int | None:
        x, y, z = pos_tuple(state)
        return self.nearest_known_water_distance((x + dx, y + dy, z))

    def target_key(self, state: dict, dx: int, dy: int) -> tuple[int, int, int]:
        x, y, z = pos_tuple(state)
        return (x + dx, y + dy, z)

    def is_known_blocked_edge(self, state: dict, dx: int, dy: int) -> bool:
        return self.edge_key(state, dx, dy) in self.blocked_edges

    def is_known_hazard(self, state: dict, dx: int, dy: int) -> bool:
        return self.target_key(state, dx, dy) in self.hazard_tiles

    def refresh_stall_escape(self, state: dict) -> None:
        current = pos_tuple(state)
        if self.looping() or self.no_progress_streak >= 2:
            self.stall_avoid_tiles.update(list(self.recent_positions)[-8:])
        elif self.stall_avoid_tiles and current not in self.stall_avoid_tiles:
            self.stall_avoid_tiles.clear()

    def record_action(self, state_before: dict, choice: dict, outcome: str,
                      result: dict | None = None) -> None:
        self.recent_actions.append({
            "action": choice.get("action"),
            "dx": choice.get("dx"),
            "dy": choice.get("dy"),
            "outcome": outcome,
        })
        if choice.get("action") == "move_one_tile" and outcome == "blocked":
            try:
                dx = int(choice.get("dx", 0))
                dy = int(choice.get("dy", 0))
                self.blocked_edges.add(self.edge_key(state_before, dx, dy))
                error = str((result or {}).get("error") or "")
                if (
                    bool(choice.get("movement_hazard"))
                    or bool(choice.get("dangerous"))
                    or bool(choice.get("deep_water"))
                    or "dangerous_tile" in error
                    or "deep_water" in error
                ):
                    self.hazard_tiles.add(self.target_key(state_before, dx, dy))
            except Exception:
                pass

        before = (result or {}).get("before") or state_before or {}
        after = (result or {}).get("after") or before
        progress = outcome in {"moved", "opened", "pickup_verified"}
        if not progress:
            try:
                progress = (
                    pos_tuple(before) != pos_tuple(after)
                    or int(after.get("inventory_count", 0) or 0) > int(before.get("inventory_count", 0) or 0)
                    or int(after.get("stamina", 0) or 0) > int(before.get("stamina", 0) or 0) + 10
                    or int(after.get("stored_kcal", 0) or 0) > int(before.get("stored_kcal", 0) or 0) + 10
                    or int(after.get("hunger", 0) or 0) < int(before.get("hunger", 0) or 0) - 4
                    or int(after.get("thirst", 0) or 0) < int(before.get("thirst", 0) or 0) - 4
                )
            except Exception:
                progress = False

        goal_id = self.active_goal_id
        if progress:
            self.no_progress_streak = 0
            if goal_id:
                self.goal_no_progress[goal_id] = 0
            self.progress_epoch += 1
        else:
            self.no_progress_streak += 1
            if goal_id:
                self.goal_no_progress[goal_id] = self.goal_no_progress.get(goal_id, 0) + 1


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

@dataclass
class PlanStep:
    kind: str
    target: dict | None = None
    params: dict = field(default_factory=dict)
    completion: dict = field(default_factory=dict)
    failure_count: int = 0
    last_failure_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "target": self.target,
            "params": self.params,
            "completion": self.completion,
            "failure_count": self.failure_count,
            "last_failure_reason": self.last_failure_reason,
        }

@dataclass
class Plan:
    plan_id: str
    goal_id: str
    intention: str
    steps: list[PlanStep]
    step_index: int = 0
    created_turn: int = 0
    interruption_conditions: list[str] = field(default_factory=list)
    planner_reason: str = ""
    provenance: str = "qwen_plan"

    @property
    def current_step(self) -> PlanStep | None:
        return self.steps[self.step_index] if self.step_index < len(self.steps) else None

    @property
    def completed(self) -> bool:
        return self.current_step is None

    def advance(self) -> None:
        self.step_index += 1

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "goal_id": self.goal_id,
            "intention": self.intention,
            "step_index": self.step_index,
            "created_turn": self.created_turn,
            "interruption_conditions": self.interruption_conditions,
            "planner_reason": self.planner_reason,
            "provenance": self.provenance,
            "steps": [step.to_dict() for step in self.steps],
        }

class DashboardFeed:
    def __init__(self) -> None:
        self.mission_path = BRIDGE / "nova-mission.txt"
        self.mind_path = BRIDGE / "nova-mind.txt"
        self.status_path = BRIDGE / "nova-status.txt"

    @staticmethod
    def _clean(text: object, limit: int = 180) -> str:
        value = " ".join(str(text or "").replace("\n", " ").split())
        return value[:limit]

    def update(self, state: dict, wm: WorldModel, plan: Plan | None,
               matched_lessons: list[dict] | None = None,
               blocker: str = "") -> None:
        phase = mission_phase(state, wm)
        goal = plan.goal_id if plan else (wm.active_goal_id or "reassess")
        intention = plan.intention if plan else (wm.active_intention or "reassess situation")
        step = "none"
        if plan and plan.current_step:
            step = f"{plan.step_index + 1}/{len(plan.steps)} {plan.current_step.kind}"
        targets = known_structure_targets(state, wm)
        target_line = "none known"
        if targets:
            t = targets[0]
            target_line = f"{t.get('kind')} {t.get('terrain')} d={t.get('distance')}"

        mission_lines = [
            f"MISSION: {self._clean(SURVIVAL_MISSION, 120)}",
            f"PHASE: {phase}",
            f"GOAL: {goal}",
            f"TARGET: {self._clean(target_line, 120)}",
        ]
        mind_lines = [
            f"INTENT: {self._clean(intention, 150)}",
            f"ASSESS: {self._clean(wm.last_planner_assessment or 'using deterministic execution', 150)}",
            f"NEXT: {self._clean(wm.last_planner_next_step or step, 150)}",
            f"BLOCKER: {self._clean(blocker or wm.last_planner_blocker or 'none', 150)}",
            f"PLAN STEP: {self._clean(step, 120)}",
        ]
        hostiles = [
            c for c in state.get("nearby_creatures", [])
            if str(c.get("attitude", "")).lower() == "hostile"
        ]
        lesson_line = "none"
        if matched_lessons:
            lesson_line = ", ".join(str(m.get("lesson_id")) for m in matched_lessons[:2])
        status_lines = [
            f"NEEDS: hunger={state.get('hunger')} thirst={state.get('thirst')} stamina={state.get('stamina')}/{state.get('stamina_max')}",
            f"PLACE: {'indoors' if state.get('indoors') else 'outdoors'} | hostiles={len(hostiles)} | landmarks={len(wm.known_landmarks)}",
            f"LAST: {self._clean(wm.last_action_summary or 'none yet', 150)}",
            f"MEMORY: {self._clean(lesson_line, 120)}",
        ]
        try:
            write_text_atomic(self.mission_path, "\n".join(mission_lines) + "\n")
            write_text_atomic(self.mind_path, "\n".join(mind_lines) + "\n")
            write_text_atomic(self.status_path, "\n".join(status_lines) + "\n")
        except OSError:
            pass

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
        if t and t.get("openable"):
            doors.append({"direction": name, "terrain": t.get("terrain", "")})
    adjacent_terrain = []
    for name, (dx, dy) in MOVE_DIRECTIONS.items():
        t = tiles.get((dx, dy))
        if not t:
            continue
        if t.get("passable"):
            open_moves.append(name)
        adjacent_terrain.append({
            "direction": name,
            "terrain": t.get("terrain", ""),
            "passable": bool(t.get("passable")),
            "swimmable": bool(t.get("swimmable")),
            "deep_water": bool(t.get("deep_water")),
            "dangerous": bool(t.get("dangerous")),
            "movement_hazard": bool(t.get("movement_hazard")),
            "special_movement": bool(t.get("special_movement")),
            "move_cost": int(t.get("move_cost", 0) or 0),
        })

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
        "adjacent_terrain": adjacent_terrain,
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

def need_profile(state: dict) -> dict:
    hunger = int(state.get("hunger", 0) or 0)
    thirst = int(state.get("thirst", 0) or 0)
    stamina = int(state.get("stamina", 0) or 0)
    stamina_max = max(1, int(state.get("stamina_max", 1) or 1))
    return {
        "hunger": hunger,
        "thirst": thirst,
        "stamina_ratio": stamina / stamina_max,
        "eat_needed": hunger >= EAT_NEED_HUNGER,
        "drink_needed": thirst >= DRINK_NEED_THIRST,
    }

def lesson_conditions(state: dict) -> dict:
    stamina = int(state.get("stamina", 0) or 0)
    stamina_max = max(1, int(state.get("stamina_max", 1) or 1))
    hostiles = []
    for creature in state.get("nearby_creatures", []) or []:
        if str(creature.get("attitude", "")).lower() != "hostile":
            continue
        try:
            distance = max(abs(int(creature.get("dx", 99))), abs(int(creature.get("dy", 99))))
        except Exception:
            distance = 99
        if distance <= THREAT_RANGE_TILES:
            hostiles.append(creature)
    return {
        "indoors": bool(state.get("indoors")),
        "night": bool(state.get("is_night", False)),
        "hostile_nearby": bool(hostiles),
        "stamina_low": stamina / stamina_max < STAMINA_LOW_RATIO,
        "hunger_critical": int(state.get("hunger", 0) or 0) >= CRITICAL_NEED_THRESHOLD,
        "thirst_critical": int(state.get("thirst", 0) or 0) >= CRITICAL_NEED_THRESHOLD,
        "pain_present": int(state.get("pain", 0) or 0) > 0,
    }

def make_death_lesson(life: "LifeTelemetry", final_state: dict) -> dict | None:
    if not life.last_actions:
        return None
    at_death_action = str(life.last_actions[-1].get("action") or "").strip()
    if not at_death_action:
        return None
    conditions = lesson_conditions(final_state)
    phrases = []
    if conditions["hostile_nearby"]:
        phrases.append("a hostile was within 3 tiles")
    if conditions["stamina_low"]:
        phrases.append("stamina was low")
    if conditions["hunger_critical"]:
        phrases.append("hunger was critical")
    if conditions["thirst_critical"]:
        phrases.append("thirst was critical")
    if conditions["pain_present"]:
        phrases.append("Nova was already in pain")
    phrases.append("Nova was indoors" if conditions["indoors"] else "Nova was outdoors")
    if conditions["night"]:
        phrases.append("it was night")
    context = ", ".join(phrases[:4])
    return {
        "schema_version": LESSON_SCHEMA_VERSION,
        "lesson_id": f"death-{life.life_id}",
        "source_life_id": life.life_id,
        "source_life_number": life.life_number,
        "created_at": utc_now(),
        "text": f"Life ended while taking {at_death_action}; {context}.",
        "conditions": conditions,
        "at_death_action": at_death_action,
    }

def load_lessons(limit: int = 100) -> list[dict]:
    if not LESSON_PATH.exists():
        return []
    try:
        lines = LESSON_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    lessons = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        if int(item.get("schema_version", -1)) != LESSON_SCHEMA_VERSION:
            continue
        lessons.append(item)
    return lessons[-max(1, limit):]

def append_lesson(lesson: dict) -> bool:
    lesson_id = str(lesson.get("lesson_id") or "")
    if not lesson_id:
        return False
    if any(str(item.get("lesson_id")) == lesson_id for item in load_lessons(500)):
        return False
    LESSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LESSON_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(lesson, ensure_ascii=False, separators=(",", ":")) + "\n")
    return True

def lesson_match_count(recorded: dict, current: dict) -> int:
    # False/false hazard matches are deliberately not counted: otherwise
    # ordinary safe states would match almost every old death. Indoor/outdoor
    # is the one neutral context bit that is allowed to count either way.
    score = 0
    if "indoors" in recorded and bool(recorded.get("indoors")) == bool(current.get("indoors")):
        score += 1
    for key in (
        "night", "hostile_nearby", "stamina_low",
        "hunger_critical", "thirst_critical", "pain_present",
    ):
        if recorded.get(key) is True and current.get(key) is True:
            score += 1
    return score

def apply_lesson_bias(state: dict, actions: list[dict],
                      lessons: list[dict]) -> tuple[list[dict], list[dict]]:
    current = lesson_conditions(state)
    matches = []
    for lesson in lessons:
        recorded = lesson.get("conditions") or {}
        score = lesson_match_count(recorded, current)
        if score < LESSON_MATCH_THRESHOLD:
            continue
        action_name = str(lesson.get("at_death_action") or "")
        if not action_name or not any(a.get("action") == action_name for a in actions):
            continue
        matches.append({
            "lesson_id": lesson.get("lesson_id"),
            "text": lesson.get("text"),
            "at_death_action": action_name,
            "match_count": score,
        })

    if not matches:
        return actions, []

    by_action: dict[str, list[dict]] = {}
    for match in matches:
        by_action.setdefault(str(match["at_death_action"]), []).append(match)

    biased = []
    for action in actions:
        item = dict(action)
        action_matches = by_action.get(str(item.get("action")), [])
        if action_matches:
            penalty = min(0.65, LESSON_ACTION_BIAS * len(action_matches))
            item["controller_score"] = max(
                0.0, float(item.get("controller_score", 0.0)) - penalty
            )
            item["lesson_bias"] = {
                "penalty": round(penalty, 3),
                "lesson_ids": [m.get("lesson_id") for m in action_matches],
                "reason": "similar conditions previously ended a life",
            }
        biased.append(item)
    return biased, matches

def hostile_distances(state: dict) -> list[tuple[int, int]]:
    positions = []
    for creature in state.get("nearby_creatures", []) or []:
        if str(creature.get("attitude", "")).lower() != "hostile":
            continue
        try:
            positions.append((int(creature.get("dx", 0)), int(creature.get("dy", 0))))
        except Exception:
            continue
    return positions

def apply_priority_ladder(state: dict, actions: list[dict]) -> tuple[list[dict], dict]:
    hostiles = hostile_distances(state)
    nearby = [
        (dx, dy) for dx, dy in hostiles
        if max(abs(dx), abs(dy)) <= THREAT_RANGE_TILES
    ]
    if nearby:
        before = min(max(abs(dx), abs(dy)) for dx, dy in nearby)
        threat_actions = []
        for action in actions:
            if action.get("action") != "move_one_tile" or action.get("hostile_on_tile"):
                continue
            ax = int(action.get("dx", 0) or 0)
            ay = int(action.get("dy", 0) or 0)
            after = min(
                max(abs(hx - ax), abs(hy - ay))
                for hx, hy in nearby
            )
            if after <= before:
                continue
            item = dict(action)
            item["controller_score"] = min(
                1.0, float(item.get("controller_score", 0.0)) + 0.25
            )
            item["threat_response"] = True
            item["threat_distance_before"] = before
            item["threat_distance_after"] = after
            threat_actions.append(item)
        if threat_actions:
            return threat_actions, {
                "tier": 1,
                "name": "threat_response",
                "exclusive": True,
                "reason": f"hostile within {THREAT_RANGE_TILES} tiles and escape movement exists",
            }

    hunger = int(state.get("hunger", 0) or 0)
    thirst = int(state.get("thirst", 0) or 0)
    critical_actions = []
    if hunger >= CRITICAL_NEED_THRESHOLD:
        critical_actions.extend(a for a in actions if a.get("action") == "eat_best_food")
    if thirst >= CRITICAL_NEED_THRESHOLD:
        critical_actions.extend(a for a in actions if a.get("action") == "drink_best")
    if critical_actions:
        return [dict(a) for a in critical_actions], {
            "tier": 2,
            "name": "critical_needs",
            "exclusive": True,
            "reason": f"critical need with a real consume action available (hunger={hunger}, thirst={thirst})",
        }

    boosted = []
    moderate_need = 20 <= hunger < CRITICAL_NEED_THRESHOLD or 20 <= thirst < CRITICAL_NEED_THRESHOLD
    hostile_free = not hostiles
    is_night_now = bool(state.get("is_night", False))
    indoors_now = bool(state.get("indoors"))
    shelter_bias_applied = False

    for action in actions:
        item = dict(action)
        score = float(item.get("controller_score", 0.0))
        name = item.get("action")

        if name == "eat_best_food" and 20 <= hunger < CRITICAL_NEED_THRESHOLD:
            score += 0.20
            item["priority_boost"] = "moderate_hunger"
        if name == "drink_best" and 20 <= thirst < CRITICAL_NEED_THRESHOLD:
            score += 0.20
            item["priority_boost"] = "moderate_thirst"

        if not indoors_now and is_night_now:
            if name == "move_one_tile" and bool(item.get("target_indoors", False)):
                score += 0.28
                item["shelter_preference"] = "enter_building_at_night"
                shelter_bias_applied = True
            elif name == "open_adjacent" and bool(item.get("target_indoors", False)):
                score += 0.18
                item["shelter_preference"] = "open_indoor_boundary_at_night"
                shelter_bias_applied = True
        elif indoors_now and hostile_free:
            if name == "move_one_tile":
                if bool(item.get("target_indoors", False)):
                    score += 0.12
                    item["shelter_preference"] = "remain_indoors"
                    shelter_bias_applied = True
                else:
                    score -= 0.18
                    item["shelter_preference"] = "avoid_leaving_safe_interior"

        if name == "pickup_consumable":
            score += 0.08
            item.setdefault("priority_boost", "resource_scouting")
        elif name == "move_one_tile" and int(item.get("visits_target", 0) or 0) == 0:
            score += 0.05
            item.setdefault("priority_boost", "exploration")

        item["controller_score"] = max(0.0, min(1.0, score))
        boosted.append(item)

    if (not indoors_now and is_night_now) or (indoors_now and hostile_free):
        return boosted, {
            "tier": 3,
            "name": "shelter_seeking",
            "exclusive": False,
            "reason": (
                "outdoors at night: prefer entering shelter"
                if not indoors_now and is_night_now
                else "indoors and hostile-free: prefer remaining inside"
            ),
            "bias_applied": shelter_bias_applied,
            "moderate_need_boost": moderate_need,
        }

    return boosted, {
        "tier": 4,
        "name": "default",
        "exclusive": False,
        "reason": "resource scouting and exploration fallback",
        "moderate_need_boost": moderate_need,
    }

def known_storable_consumables(state: dict, wm: WorldModel | None) -> list[dict]:
    if wm is None:
        return visible_storable_consumables(state)
    px, py, pz = pos_tuple(state)
    found = []
    for (gx, gy, gz), tile in wm.known_tiles.items():
        if gz != pz:
            continue
        dx = gx - px
        dy = gy - py
        # Keep this a local executive target, not a global path planner yet.
        if abs(dx) > 16 or abs(dy) > 16:
            continue
        for food in (tile.get("ground_consumables") or []):
            if not isinstance(food, dict) or not bool(food.get("storable_without_wield", False)):
                continue
            name = str(food.get("name", "")).strip()
            if not name:
                continue
            found.append({
                "dx": dx,
                "dy": dy,
                "gx": gx,
                "gy": gy,
                "gz": gz,
                "name": name,
                "nutrition": int(food.get("nutrition", 0) or 0),
                "quench": int(food.get("quench", 0) or 0),
                "distance": abs(dx) + abs(dy),
            })
    return found

def known_structure_targets(state: dict, wm: WorldModel | None) -> list[dict]:
    if wm is None:
        return []
    px, py, pz = pos_tuple(state)
    targets = []
    seen = set()

    for (gx, gy, gz), landmark in wm.known_landmarks.items():
        if gz != pz or (gx, gy, gz) in wm.failed_landmarks:
            continue
        dx = gx - px
        dy = gy - py
        distance = max(abs(dx), abs(dy))
        if distance <= 1 or distance > 64:
            continue
        key = (gx, gy, gz)
        seen.add(key)
        targets.append({
            "gx": gx,
            "gy": gy,
            "gz": gz,
            "dx": dx,
            "dy": dy,
            "distance": distance,
            "terrain": str(landmark.get("terrain", "")),
            "kind": str(landmark.get("kind", "shelter_interior")),
            "visited": int(wm.visits.get(key, 0) or 0) > 0,
            "strategic_landmark": True,
        })

    for (gx, gy, gz), tile in wm.known_tiles.items():
        if (gx, gy, gz) in seen:
            continue
        if gz != pz:
            continue
        is_boundary = bool(tile.get("openable"))
        is_interior = bool(tile.get("indoors")) and bool(tile.get("passable"))
        if not (is_boundary or is_interior):
            continue
        visits = int(wm.visits.get((gx, gy, gz), 0) or 0)
        if visits > 0 and is_interior:
            continue
        dx = gx - px
        dy = gy - py
        distance = max(abs(dx), abs(dy))
        if distance <= 1 or distance > 64:
            continue
        targets.append({
            "gx": gx,
            "gy": gy,
            "gz": gz,
            "dx": dx,
            "dy": dy,
            "distance": distance,
            "terrain": str(tile.get("terrain", "")),
            "kind": "boundary" if is_boundary else "interior",
            "visited": visits > 0,
            "strategic_landmark": False,
        })
    targets.sort(key=lambda t: (
        0 if t.get("kind") in {"boundary", "shelter_entrance"} else 1,
        1 if t.get("visited") else 0,
        int(t.get("distance", 999)),
    ))
    return targets

def known_escape_target(state: dict, wm: WorldModel | None) -> dict | None:
    if wm is None:
        return None
    px, py, pz = pos_tuple(state)
    candidates = []
    for (gx, gy, gz), tile in wm.known_tiles.items():
        if gz != pz or not bool(tile.get("passable")):
            continue
        if (
            bool(tile.get("movement_hazard"))
            or bool(tile.get("dangerous"))
            or bool(tile.get("deep_water"))
            or bool(tile.get("swimmable"))
        ):
            continue
        distance = max(abs(gx - px), abs(gy - py))
        if distance < 6 or distance > 28:
            continue
        visits = int(wm.visits.get((gx, gy, gz), 0) or 0)
        recent_penalty = 1 if (gx, gy, gz) in set(wm.trajectory_positions) else 0
        candidates.append((
            visits,
            recent_penalty,
            -distance,
            {
                "gx": gx,
                "gy": gy,
                "gz": gz,
                "distance": distance,
                "terrain": str(tile.get("terrain", "")),
            },
        ))
    if not candidates:
        return None
    candidates.sort(key=lambda row: (row[0], row[1], row[2]))
    return candidates[0][3]

def strategic_progress_summary(wm: WorldModel) -> dict:
    trajectory = list(wm.trajectory_positions)
    if len(trajectory) < 2:
        return {
            "samples": len(trajectory),
            "path_distance": 0,
            "net_displacement": 0,
            "efficiency": 1.0,
            "stall_active": wm.strategic_stall_active,
        }
    path_distance = 0
    for a, b in zip(trajectory, trajectory[1:]):
        if a[2] == b[2]:
            path_distance += max(abs(b[0] - a[0]), abs(b[1] - a[1]))
    start = trajectory[0]
    end = trajectory[-1]
    net = (
        max(abs(end[0] - start[0]), abs(end[1] - start[1]))
        if start[2] == end[2] else path_distance
    )
    return {
        "samples": len(trajectory),
        "path_distance": path_distance,
        "net_displacement": net,
        "efficiency": round(net / path_distance, 3) if path_distance else 1.0,
        "stall_active": wm.strategic_stall_active,
        "stall_anchor": wm.strategic_stall_anchor,
    }

def mission_phase(state: dict, wm: WorldModel | None) -> str:
    if wm is None:
        return "survive"
    hostile = any(
        str(c.get("attitude", "")).lower() == "hostile"
        and max(abs(int(c.get("dx", 99) or 99)), abs(int(c.get("dy", 99) or 99))) <= THREAT_RANGE_TILES
        for c in state.get("nearby_creatures", [])
    )
    if hostile:
        return "escape_immediate_threat"
    needs = need_profile(state)
    if int(needs.get("thirst", 0) or 0) >= CRITICAL_NEED_THRESHOLD:
        return "solve_critical_thirst"
    if int(needs.get("hunger", 0) or 0) >= CRITICAL_NEED_THRESHOLD:
        return "solve_critical_hunger"
    if bool(state.get("indoors")):
        return "shelter_secured_maintain_supplies"
    if known_structure_targets(state, wm):
        return "reach_known_shelter"
    return "search_for_shelter"

def goal_candidates(state: dict, actions: list[dict], wm: WorldModel | None = None) -> list[dict]:
    candidates = []
    action_names = {a.get("action") for a in actions}

    needs = need_profile(state)

    if any(bool(a.get("threat_response")) for a in actions):
        return [{
            "goal_id": "evade_threat",
            "intention": "increase distance from the nearby hostile using a viable escape route",
            "supported_by": ["move_one_tile"],
            "priority": 1.0,
        }]

    if "drink_best" in action_names and needs["drink_needed"]:
        candidates.append({
            "goal_id": "reduce_thirst",
            "intention": "drink something safe to reduce thirst",
            "supported_by": ["drink_best"],
            "priority": 0.98,
        })
    if "eat_best_food" in action_names and needs["eat_needed"]:
        candidates.append({
            "goal_id": "reduce_hunger",
            "intention": "eat something safe to reduce hunger",
            "supported_by": ["eat_best_food"],
            "priority": 0.96,
        })
    known_resources = known_storable_consumables(state, wm)
    known_structures = known_structure_targets(state, wm)
    phase = mission_phase(state, wm)
    nonadjacent_resources = [r for r in known_resources if int(r.get("distance", 99)) > 1]
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

    # Mission hierarchy: ordinary exploration is never the top-level purpose.
    # Outdoors, Nova either moves toward known shelter or searches for shelter.
    if not bool(state.get("indoors")) and known_structures and "move_one_tile" in action_names:
        target = known_structures[0]
        return [{
            "goal_id": "approach_known_structure",
            "intention": f"secure shelter by moving toward the spotted {target.get('terrain') or 'structure'}",
            "supported_by": ["move_one_tile", "open_adjacent"],
            "priority": 1.0,
            "target": target,
            "mission_phase": phase,
        }]

    navigation_judgment = []
    if any(a.get("navigation_override") == "leave_shoreline" for a in actions):
        navigation_judgment.append({
            "goal_id": "leave_shoreline",
            "intention": "move inland because recent exploration has stayed too close to the shoreline",
            "supported_by": ["move_one_tile"],
            "priority": 0.97,
            "navigation_override": True,
        })

    if known_structures and "move_one_tile" in action_names:
        target = known_structures[0]
        navigation_judgment.append({
            "goal_id": "approach_known_structure",
            "intention": f"move toward a previously spotted {target.get('terrain') or 'structure'} instead of continuing blind frontier walking",
            "supported_by": ["move_one_tile", "open_adjacent"],
            "priority": 0.99 if (wm and (wm.strategic_stall_active or wm.shoreline_escape_active)) else 0.84,
            "target": target,
        })

    if wm and wm.strategic_stall_active and "move_one_tile" in action_names:
        escape_target = known_escape_target(state, wm)
        navigation_judgment.append({
            "goal_id": "break_exploration_stall",
            "intention": "break the long low-displacement exploration pattern and commit to a different heading",
            "supported_by": ["move_one_tile"],
            "priority": 0.95,
            "target": escape_target,
            "strategic_stall": strategic_progress_summary(wm),
        })

    if wm and (wm.strategic_stall_active or wm.shoreline_escape_active) and navigation_judgment:
        return navigation_judgment

    if any(a.get("stall_escape") for a in actions):
        candidates.append({
            "goal_id": "escape_local_stall",
            "intention": "leave the recently repeated local area using a safe route",
            "supported_by": ["move_one_tile"],
            "priority": 0.94,
        })
    if any(a.get("action") == "move_one_tile" and int(a.get("visits_target", 0) or 0) == 0 for a in actions):
        if not bool(state.get("indoors")) and not known_structures:
            candidates.append({
                "goal_id": "search_for_shelter",
                "intention": "search deliberately for usable shelter; frontier movement is only the means, not the goal",
                "supported_by": ["move_one_tile", "open_adjacent"],
                "priority": 0.82,
                "mission_phase": phase,
            })
        else:
            candidates.append({
                "goal_id": "explore_frontier",
                "intention": "explore nearby unvisited space to improve survival options",
                "supported_by": ["move_one_tile", "open_adjacent"],
                "priority": 0.60,
                "mission_phase": phase,
            })
    stamina_ratio = float(needs["stamina_ratio"])
    wait_actions = [a for a in actions if a.get("action") == "wait_one_turn"]
    if wait_actions and stamina_ratio < 0.70:
        candidates.append({
            "goal_id": "recover_stamina",
            "intention": "pause briefly because stamina is genuinely low",
            "supported_by": ["wait_one_turn"],
            "priority": min(0.90, 0.45 + (0.70 - stamina_ratio)),
        })
    elif any(a.get("wait_reason") == "stalled" for a in wait_actions):
        candidates.append({
            "goal_id": "reassess_stall",
            "intention": "wait once and reassess because no safe local movement is currently available",
            "supported_by": ["wait_one_turn"],
            "priority": 0.10,
        })

    if not candidates:
        candidates.append({
            "goal_id": "safe_progress",
            "intention": "make the safest available local progress",
            "supported_by": sorted(action_names),
            "priority": 0.20,
        })

    for candidate in candidates:
        supported = set(candidate.get("supported_by") or [])
        penalties = [
            float((a.get("lesson_bias") or {}).get("penalty", 0.0))
            for a in actions if a.get("action") in supported and a.get("lesson_bias")
        ]
        if penalties:
            penalty = min(0.35, max(penalties))
            candidate["priority"] = max(0.0, float(candidate.get("priority", 0.0)) - penalty)
            candidate["lesson_bias"] = round(penalty, 3)
    return candidates

def choose_fallback_goal(candidates: list[dict]) -> dict:
    return max(candidates, key=lambda g: float(g.get("priority", 0.0)))

def goal_still_supported(wm: WorldModel, actions: list[dict], state: dict) -> bool:
    if not wm.active_goal_id:
        return False
    for goal in goal_candidates(state, actions, wm):
        if goal.get("goal_id") == wm.active_goal_id:
            supported = set(goal.get("supported_by") or [])
            return any(a.get("action") in supported for a in actions)
    return False

def available_actions(state: dict, wm: WorldModel) -> list[dict]:
    actions = []
    tiles = tile_map(state)
    hostiles = hostile_positions(state)
    loop = wm.looping()
    wm.refresh_stall_escape(state)
    visible_resources = visible_storable_consumables(state)
    known_resources = known_storable_consumables(state, wm)
    current_water_distance = wm.nearest_known_water_distance(pos_tuple(state))

    for name, (dx, dy) in CARDINALS.items():
        t = tiles.get((dx, dy))
        if not t or not t.get("openable"):
            continue
        terrain = str(t.get("terrain", ""))
        lower_terrain = terrain.lower()
        is_curtain = "curtain" in lower_terrain
        if is_curtain:
            score = 0.48 + (0.08 if state.get("indoors") else 0.0)
            label = f"open {name} curtains"
            progress = "improves visibility but is not an exit"
        else:
            score = 0.78 + (0.12 if state.get("indoors") else 0.0) + (0.08 if loop else 0.0)
            label = f"open {name} {terrain or 'door'}"
            progress = "reveals/accesses a new boundary"
        actions.append({
            "action": "open_adjacent", "dx": dx, "dy": dy,
            "label": label,
            "controller_score": min(1.0, score),
            "progress": progress,
            "target_indoors": bool(t.get("indoors", False)),
        })

    for name, (dx, dy) in MOVE_DIRECTIONS.items():
        t = tiles.get((dx, dy))
        if not t:
            continue
        visits = wm.visit_count_target(state, dx, dy)
        backtrack = wm.is_immediate_backtrack(state, dx, dy)

        if t.get("passable"):
            # Hard safety floor: confirmed movement hazards are never ordinary
            # frontier. This is generic, not water-specific: CDDA classifies
            # dangerous terrain and Nova remembers those absolute tiles.
            movement_hazard = (
                bool(t.get("movement_hazard"))
                or bool(t.get("deep_water"))
                or bool(t.get("dangerous"))
                or wm.is_known_hazard(state, dx, dy)
            )
            if movement_hazard:
                continue

            # A native CDDA refusal is evidence. Do not hammer the same
            # source->target edge repeatedly just because terrain is nominally passable.
            if wm.is_known_blocked_edge(state, dx, dy):
                continue

            shallow_water = bool(t.get("swimmable"))
            special_movement = bool(t.get("special_movement")) or shallow_water
            move_cost = int(t.get("move_cost", 0) or 0)
            novelty = 1.0 / (1.0 + visits)
            score = 0.58 + 0.30 * novelty
            if special_movement:
                # Slow/special terrain (shallow water, rubble, etc.) remains
                # traversable but does not count as attractive frontier merely
                # because it is unvisited. Ordinary dry/easy ground wins first.
                score -= 0.30
            if shallow_water:
                score -= 0.20
            if move_cost > 2:
                score -= min(0.20, 0.03 * (move_cost - 2))

            target_water_distance = wm.target_water_distance(state, dx, dy)
            shoreline_delta = 0
            if current_water_distance is not None and target_water_distance is not None:
                shoreline_delta = target_water_distance - current_water_distance
                if wm.shoreline_escape_active:
                    if shoreline_delta > 0:
                        score += min(0.75, 0.30 * shoreline_delta)
                    elif shoreline_delta < 0:
                        score -= 0.85
                    else:
                        score -= 0.20
            resource_distance_delta = 0
            nearest_resource = None
            if known_resources:
                nearest_resource = max(
                    known_resources,
                    key=lambda r: resource_need_weight(state, r) - 0.03 * float(r.get("distance", 0))
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
                "target_indoors": bool(t.get("indoors", False)),
                "swimmable": shallow_water,
                "deep_water": bool(t.get("deep_water")),
                "dangerous": bool(t.get("dangerous")),
                "movement_hazard": movement_hazard,
                "special_movement": special_movement,
                "move_cost": move_cost,
                "navigation_rank": 2 if shallow_water else (1 if special_movement else 0),
                "stall_recent_target": wm.target_key(state, dx, dy) in wm.stall_avoid_tiles,
                "shoreline_escape": wm.shoreline_escape_active,
                "water_distance_before": current_water_distance,
                "water_distance_after": target_water_distance,
                "shoreline_distance_delta": shoreline_delta,
                "visits_target": visits,
                "immediate_backtrack": backtrack,
                "hostile_on_tile": (dx, dy) in hostiles,
                "resource_distance_delta": resource_distance_delta,
                "resource_target": nearest_resource,
                "controller_score": max(0.0, min(1.0, score)),
                "progress": progress,
            })

    # Macro shoreline escape. Local movement can be valid on every step while
    # still making strategically useless progress parallel to a coast. When
    # sustained water adjacency is detected, and at least one safe step increases
    # distance from known water, ordinary movement is temporarily narrowed to
    # those away-from-water choices until Nova is well clear of the shoreline.
    if wm.shoreline_escape_active:
        move_actions = [a for a in actions if a.get("action") == "move_one_tile"]
        away_moves = [
            a for a in move_actions
            if int(a.get("shoreline_distance_delta", 0) or 0) > 0
        ]
        if away_moves:
            away_keys = {
                (int(a.get("dx", 0) or 0), int(a.get("dy", 0) or 0))
                for a in away_moves
            }
            narrowed = []
            for action in actions:
                if action.get("action") != "move_one_tile":
                    narrowed.append(action)
                    continue
                key = (int(action.get("dx", 0) or 0), int(action.get("dy", 0) or 0))
                if key in away_keys:
                    item = dict(action)
                    item["navigation_override"] = "leave_shoreline"
                    item["controller_score"] = min(
                        1.0, float(item.get("controller_score", 0.0)) + 0.25
                    )
                    narrowed.append(item)
            actions = narrowed

    # Generic stall breaker. If Nova has been pacing or making no net
    # progress, and at least one safe move exits the recent local cluster,
    # remove the old-cluster moves from this decision cycle. This makes
    # "abandon plan" actually reroute instead of immediately retrying the
    # same shoreline/corner from another angle.
    move_actions = [a for a in actions if a.get("action") == "move_one_tile"]
    if wm.stall_avoid_tiles and move_actions:
        escape_moves = [a for a in move_actions if not a.get("stall_recent_target")]
        if escape_moves:
            escape_keys = {
                (int(a.get("dx", 0) or 0), int(a.get("dy", 0) or 0))
                for a in escape_moves
            }
            filtered = []
            for action in actions:
                if action.get("action") != "move_one_tile":
                    filtered.append(action)
                    continue
                key = (int(action.get("dx", 0) or 0), int(action.get("dy", 0) or 0))
                if key in escape_keys:
                    item = dict(action)
                    item["stall_escape"] = True
                    item["controller_score"] = min(
                        1.0, float(item.get("controller_score", 0.0)) + 0.20
                    )
                    filtered.append(item)
            actions = filtered

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
    needs = need_profile(state)
    hunger = int(needs["hunger"])
    thirst = int(needs["thirst"])
    if needs["eat_needed"] and any(int(x.get("nutrition", 0) or 0) > 0 for x in consumables):
        actions.append({
            "action": "eat_best_food",
            "label": "eat the best safe carried food because hunger is meaningful",
            "controller_score": min(0.90, 0.45 + max(0, hunger - EAT_NEED_HUNGER) / 180.0),
            "need_evidence": {"hunger": hunger, "threshold": EAT_NEED_HUNGER},
        })
    if needs["drink_needed"] and any(int(x.get("quench", 0) or 0) > 0 for x in consumables):
        actions.append({
            "action": "drink_best",
            "label": "drink the best safe carried drink because thirst is meaningful",
            "controller_score": min(0.90, 0.45 + max(0, thirst - DRINK_NEED_THIRST) / 160.0),
            "need_evidence": {"thirst": thirst, "threshold": DRINK_NEED_THIRST},
        })

    stamina = int(state.get("stamina", 0) or 0)
    stamina_max = max(1, int(state.get("stamina_max", 1) or 1))
    stamina_ratio = stamina / stamina_max
    if stamina_ratio < 0.7:
        actions.append({
            "action": "wait_one_turn",
            "label": "pause briefly to recover genuinely low stamina",
            "controller_score": 0.2 if stamina_ratio >= 0.25 else 0.8,
            "wait_reason": "recover_stamina",
        })
    elif not actions:
        actions.append({
            "action": "wait_one_turn",
            "label": "wait once and reassess local options",
            "controller_score": 0.05,
            "wait_reason": "stalled",
        })

    return actions

def needs_qwen_judgment(state: dict, wm: WorldModel, actions: list[dict]) -> bool:
    # Qwen is for judgment, not footsteps. Empty, repetitive traversal should
    # stay fast until something appears that can materially change the choice.
    if wm.shoreline_escape_active or wm.strategic_stall_active:
        return True
    if state.get("strategic_landmarks"):
        return True
    if wm.looping() or wm.no_progress_streak >= 2:
        return True
    if any(str(c.get("attitude", "")).lower() == "hostile"
           for c in state.get("nearby_creatures", [])):
        return True
    if any(t.get("openable") for t in state.get("local_tiles", [])):
        return True
    if any(a.get("action") in {"pickup_consumable", "eat_best_food", "drink_best"}
           for a in actions):
        return True
    if any(a.get("lesson_bias") or a.get("threat_response") or a.get("shelter_preference")
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

def fast_frontier_plan(state: dict, actions: list[dict]) -> Plan:
    return Plan(
        plan_id=uuid.uuid4().hex,
        goal_id="explore_frontier",
        intention="move quickly through uneventful terrain until something meaningful requires judgment",
        steps=[PlanStep(
            kind="explore",
            params={"max_successful_moves": FAST_FRONTIER_MOVES, "successful_moves": 0},
            completion={"type": "explore_budget"},
        )],
        created_turn=current_turn(state),
        interruption_conditions=[
            "meaningful_judgment_event",
            "urgent_need_floor_crossing",
            "hostile_within_5",
            "current_step_failed_twice",
        ],
        planner_reason="deterministic fast frontier: nothing currently requires Qwen judgment",
        provenance="fast_frontier",
    )

def deterministic_safety(state: dict, actions: list[dict]):
    def find(name: str):
        return next((a for a in actions if a.get("action") == name), None)
    thirst = int(state.get("thirst", 0) or 0)
    hunger = int(state.get("hunger", 0) or 0)
    stamina = int(state.get("stamina", 0) or 0)
    stamina_max = max(1, int(state.get("stamina_max", 1) or 1))

    if thirst >= CRITICAL_NEED_THRESHOLD and find("drink_best"):
        return find("drink_best"), f"critical thirst ({thirst})"
    if hunger >= CRITICAL_NEED_THRESHOLD and find("eat_best_food"):
        return find("eat_best_food"), f"critical hunger ({hunger})"
    return None

def compact_world(state: dict, wm: WorldModel, actions: list[dict]) -> dict:
    sit = situation_summary(state, wm)
    return {
        "mission": SURVIVAL_MISSION,
        "mission_phase": mission_phase(state, wm),
        "situation": sit,
        "needs": {k: state.get(k) for k in (
            "hunger", "thirst", "sleepiness", "stamina", "stamina_max",
            "pain", "morale", "stored_kcal", "healthy_kcal"
        )},
        "position": state.get("position"),
        "activity": state.get("activity"),
        "is_night": bool(state.get("is_night", False)),
        "priority_ladder": wm.priority_context,
        "active_goal_id": wm.active_goal_id or None,
        "active_intention": wm.active_intention or None,
        "no_progress_streak": wm.no_progress_streak,
        "shoreline_context": {
            "escape_active": wm.shoreline_escape_active,
            "recent_water_distance": list(wm.recent_water_distance)[-16:],
            "current_water_distance": wm.nearest_known_water_distance(pos_tuple(state)),
            "clear_distance": SHORELINE_ESCAPE_DISTANCE,
        },
        "strategic_progress": strategic_progress_summary(wm),
        "known_structure_targets": known_structure_targets(state, wm)[:5],
        "active_goal_no_progress": wm.goal_no_progress.get(wm.active_goal_id, 0),
        "goal_candidates": goal_candidates(state, actions, wm),
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

def stable_direction_rank(dx: int, dy: int) -> int:
    order = list(MOVE_DIRECTIONS.values())
    try:
        return order.index((dx, dy))
    except ValueError:
        return len(order)

def current_turn(state: dict) -> int:
    return int(state.get("turn", 0) or 0)

def absolute_target_from_relative(state: dict, dx: int, dy: int) -> dict:
    x, y, z = pos_tuple(state)
    return {"x": x + dx, "y": y + dy, "z": z}

def plan_step_complete_before_action(step: PlanStep, state: dict) -> bool:
    if step.kind == "go_to" and step.target:
        x, y, z = pos_tuple(state)
        tx = int(step.target.get("x", x))
        ty = int(step.target.get("y", y))
        tz = int(step.target.get("z", z))
        radius = int(step.params.get("arrival_radius", 0) or 0)
        return z == tz and max(abs(tx - x), abs(ty - y)) <= radius
    if step.kind == "rest":
        stamina = int(state.get("stamina", 0) or 0)
        stamina_max = max(1, int(state.get("stamina_max", 1) or 1))
        return stamina / stamina_max >= float(step.params.get("until_ratio", 0.90))
    return False

def select_interaction(actions: list[dict], action_name: str,
                       item_name: str | None = None,
                       target: dict | None = None,
                       state: dict | None = None) -> dict | None:
    candidates = [a for a in actions if a.get("action") == action_name]
    if item_name:
        candidates = [a for a in candidates if a.get("item_name") == item_name]
    if target and state and action_name in {"pickup_consumable", "open_adjacent"}:
        x, y, _ = pos_tuple(state)
        tx = int(target.get("x", x))
        ty = int(target.get("y", y))
        dx, dy = tx - x, ty - y
        candidates = [a for a in candidates if a.get("dx") == dx and a.get("dy") == dy]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda a: (
            float(a.get("controller_score", 0.0)),
            -int(a.get("visits_target", 0) or 0),
            -stable_direction_rank(int(a.get("dx", 0) or 0), int(a.get("dy", 0) or 0)),
        ),
    )

def deterministic_move_toward(step: PlanStep, state: dict, actions: list[dict]) -> dict | None:
    if not step.target:
        return None
    x, y, z = pos_tuple(state)
    tx = int(step.target.get("x", x))
    ty = int(step.target.get("y", y))
    tz = int(step.target.get("z", z))
    if z != tz:
        return None
    radius = int(step.params.get("arrival_radius", 0) or 0)
    before = max(abs(tx - x), abs(ty - y))
    if before <= radius:
        return None

    improving = []
    lateral = []
    for action in actions:
        if action.get("action") != "move_one_tile" or action.get("hostile_on_tile"):
            continue
        dx = int(action.get("dx", 0) or 0)
        dy = int(action.get("dy", 0) or 0)
        after = max(abs(tx - (x + dx)), abs(ty - (y + dy)))
        row = (
            after,
            int(action.get("visits_target", 0) or 0),
            1 if action.get("immediate_backtrack") else 0,
            -float(action.get("controller_score", 0.0)),
            stable_direction_rank(dx, dy),
            action,
        )
        if after < before:
            improving.append(row)
        elif after == before:
            lateral.append(row)
    candidates = improving or lateral
    if not candidates:
        return None
    candidates.sort(key=lambda row: row[:-1])
    choice = dict(candidates[0][-1])
    choice["reason"] = f"executor step toward committed target ({tx},{ty},{tz})"
    choice["provenance"] = "plan_executor"
    return choice

def deterministic_explore_choice(step: PlanStep, actions: list[dict]) -> dict | None:
    moves = [
        a for a in actions
        if a.get("action") == "move_one_tile" and not a.get("hostile_on_tile")
    ]
    if moves:
        moves.sort(key=lambda a: (
            int(a.get("navigation_rank", 0) or 0),
            int(a.get("visits_target", 0) or 0),
            1 if a.get("immediate_backtrack") else 0,
            -float(a.get("controller_score", 0.0)),
            stable_direction_rank(int(a.get("dx", 0) or 0), int(a.get("dy", 0) or 0)),
        ))
        choice = dict(moves[0])
        choice["reason"] = "executor frontier step for committed explore plan"
        choice["provenance"] = "plan_executor"
        return choice

    doors = [a for a in actions if a.get("action") == "open_adjacent"]
    if doors:
        doors.sort(key=lambda a: (
            -float(a.get("controller_score", 0.0)),
            stable_direction_rank(int(a.get("dx", 0) or 0), int(a.get("dy", 0) or 0)),
        ))
        choice = dict(doors[0])
        choice["reason"] = "executor opens boundary because local frontier is exhausted"
        choice["provenance"] = "plan_executor"
        return choice
    return None

def execute_step(plan: Plan, state: dict, wm: WorldModel,
                 actions: list[dict]) -> tuple[dict | None, str]:
    step = plan.current_step
    if step is None:
        return None, "plan_complete"

    if step.kind == "go_to":
        choice = deterministic_move_toward(step, state, actions)
        return choice, "committed go_to execution" if choice else "go_to_no_progress_action"

    if step.kind == "consume":
        action_name = str(step.params.get("action", ""))
        choice = select_interaction(actions, action_name)
        if choice:
            choice = dict(choice)
            choice["reason"] = f"executor performs committed {action_name}"
            choice["provenance"] = "plan_executor"
        return choice, f"committed consume step {action_name}"

    if step.kind == "interact":
        action_name = str(step.params.get("action", ""))
        item_name = step.params.get("item_name")
        choice = select_interaction(actions, action_name, item_name, step.target, state)
        if choice:
            choice = dict(choice)
            choice["reason"] = f"executor performs committed interaction {action_name}"
            choice["provenance"] = "plan_executor"
        return choice, f"committed interact step {action_name}"

    if step.kind == "rest":
        choice = select_interaction(actions, "wait_one_turn")
        if choice:
            choice = dict(choice)
            choice["reason"] = "executor continues committed rest until stamina target"
            choice["provenance"] = "plan_executor"
        return choice, "committed rest step"

    if step.kind == "explore":
        return deterministic_explore_choice(step, actions), "committed deterministic exploration"

    return None, f"unsupported_plan_step:{step.kind}"

def synthesize_plan_from_goal(goal: dict, state: dict, wm: WorldModel,
                              actions: list[dict], planner_reason: str = "",
                              provenance: str = "qwen_plan") -> Plan:
    goal_id = str(goal.get("goal_id", "safe_progress"))
    intention = str(goal.get("intention", "make grounded progress"))
    steps: list[PlanStep] = []

    if goal_id == "evade_threat":
        steps.append(PlanStep(
            kind="explore",
            params={"max_successful_moves": 1, "successful_moves": 0},
            completion={"type": "explore_budget"},
        ))
    elif goal_id == "reduce_thirst":
        steps.append(PlanStep(
            kind="consume",
            params={"action": "drink_best"},
            completion={"type": "consumed"},
        ))
    elif goal_id == "reduce_hunger":
        steps.append(PlanStep(
            kind="consume",
            params={"action": "eat_best_food"},
            completion={"type": "consumed"},
        ))
    elif goal_id == "approach_known_structure":
        target = dict(goal.get("target") or {})
        if target:
            abs_target = {
                "x": int(target.get("gx", pos_tuple(state)[0])),
                "y": int(target.get("gy", pos_tuple(state)[1])),
                "z": int(target.get("gz", pos_tuple(state)[2])),
            }
            steps.append(PlanStep(
                kind="go_to",
                target=abs_target,
                params={"arrival_radius": 1},
                completion={"type": "arrived_near"},
            ))
            if target.get("kind") in {"boundary", "shelter_entrance"}:
                steps.append(PlanStep(
                    kind="interact",
                    target=abs_target,
                    params={"action": "open_adjacent"},
                    completion={"type": "opened"},
                ))
                steps.append(PlanStep(
                    kind="go_to",
                    target=abs_target,
                    params={"arrival_radius": 0},
                    completion={"type": "entered_threshold"},
                ))
    elif goal_id == "break_exploration_stall":
        target = dict(goal.get("target") or {})
        if target:
            steps.append(PlanStep(
                kind="go_to",
                target={
                    "x": int(target.get("gx", pos_tuple(state)[0])),
                    "y": int(target.get("gy", pos_tuple(state)[1])),
                    "z": int(target.get("gz", pos_tuple(state)[2])),
                },
                params={"arrival_radius": 1},
                completion={"type": "arrived_near"},
            ))
        else:
            steps.append(PlanStep(
                kind="explore",
                params={"max_successful_moves": 12, "successful_moves": 0},
                completion={"type": "explore_budget"},
            ))
    elif goal_id == "approach_consumable":
        target = dict(goal.get("target") or {})
        if target:
            abs_target = {
                "x": int(target.get("gx", pos_tuple(state)[0] + int(target.get("dx", 0) or 0))),
                "y": int(target.get("gy", pos_tuple(state)[1] + int(target.get("dy", 0) or 0))),
                "z": int(target.get("gz", pos_tuple(state)[2])),
            }
            steps.append(PlanStep(
                kind="go_to",
                target=abs_target,
                params={"arrival_radius": 1},
                completion={"type": "arrived_near"},
            ))
            steps.append(PlanStep(
                kind="interact",
                target=abs_target,
                params={"action": "pickup_consumable", "item_name": target.get("name")},
                completion={"type": "pickup_verified", "item_name": target.get("name")},
            ))
    elif goal_id == "acquire_consumable":
        pickups = [a for a in actions if a.get("action") == "pickup_consumable"]
        if pickups:
            pickups.sort(key=lambda a: (
                -float(a.get("controller_score", 0.0)),
                stable_direction_rank(int(a.get("dx", 0) or 0), int(a.get("dy", 0) or 0)),
            ))
            p = pickups[0]
            steps.append(PlanStep(
                kind="interact",
                target=absolute_target_from_relative(state, int(p.get("dx", 0)), int(p.get("dy", 0))),
                params={"action": "pickup_consumable", "item_name": p.get("item_name")},
                completion={"type": "pickup_verified", "item_name": p.get("item_name")},
            ))
    elif goal_id == "open_boundary":
        doors = [a for a in actions if a.get("action") == "open_adjacent"]
        if doors:
            doors.sort(key=lambda a: (
                -float(a.get("controller_score", 0.0)),
                stable_direction_rank(int(a.get("dx", 0) or 0), int(a.get("dy", 0) or 0)),
            ))
            d = doors[0]
            steps.append(PlanStep(
                kind="interact",
                target=absolute_target_from_relative(state, int(d.get("dx", 0)), int(d.get("dy", 0))),
                params={"action": "open_adjacent"},
                completion={"type": "opened"},
            ))
            steps.append(PlanStep(
                kind="explore",
                params={"max_successful_moves": 4, "successful_moves": 0},
                completion={"type": "explore_budget"},
            ))
    elif goal_id == "recover_stamina":
        steps.append(PlanStep(
            kind="rest",
            params={"until_ratio": 0.90},
            completion={"type": "stamina_ratio", "at_least": 0.90},
        ))
    elif goal_id == "reassess_stall":
        steps.append(PlanStep(
            kind="interact",
            params={"action": "wait_one_turn"},
            completion={"type": "waited_once"},
        ))
    elif goal_id == "escape_local_stall":
        steps.append(PlanStep(
            kind="explore",
            params={"max_successful_moves": 8, "successful_moves": 0},
            completion={"type": "explore_budget"},
        ))
    elif goal_id == "leave_shoreline":
        steps.append(PlanStep(
            kind="explore",
            params={"max_successful_moves": 24, "successful_moves": 0},
            completion={"type": "explore_budget"},
        ))
    elif goal_id == "search_for_shelter":
        steps.append(PlanStep(
            kind="explore",
            params={"max_successful_moves": 32, "successful_moves": 0},
            completion={"type": "explore_budget"},
        ))
    else:
        steps.append(PlanStep(
            kind="explore",
            params={"max_successful_moves": 6, "successful_moves": 0},
            completion={"type": "explore_budget"},
        ))

    if not steps:
        steps.append(PlanStep(
            kind="explore",
            params={"max_successful_moves": 4, "successful_moves": 0},
            completion={"type": "explore_budget"},
        ))

    return Plan(
        plan_id=uuid.uuid4().hex,
        goal_id=goal_id,
        intention=intention,
        steps=steps[:5],
        created_turn=current_turn(state),
        interruption_conditions=[
            "urgent_need_floor_crossing",
            "hostile_within_5",
            "current_step_failed_twice",
            "current_step_unsupported",
        ],
        planner_reason=planner_reason,
        provenance=provenance,
    )

def compact_planner_world(state: dict, wm: WorldModel, actions: list[dict]) -> dict:
    base = compact_world(state, wm, actions)
    base.pop("active_goal_id", None)
    base.pop("active_intention", None)
    base["planner_contract"] = {
        "job": "choose one feasible survival subgoal, not a tile-level action",
        "controller_executes_steps": True,
        "maximum_plan_steps": 5,
    }
    return base

def qwen_plan(model: str, state: dict, wm: WorldModel, actions: list[dict],
              trace_path: Path) -> tuple[Plan, float, dict, int]:
    goals = goal_candidates(state, actions, wm)
    world = compact_planner_world(state, wm, actions)
    instruction = (
        "You are Nova's executive planner in Cataclysm: Dark Days Ahead. "
        f"Permanent mission: {SURVIVAL_MISSION} "
        "Choose WHAT Nova should accomplish next, not which tile to step onto. "
        "The deterministic executor handles movement and ordinary execution. "
        "Choose exactly one goal_id from goal_candidates. "
        "The priority_ladder has already filtered or biased the action space; respect it. "
        "When lesson_bias appears, treat it as evidence from a previous real death and prefer a different feasible choice when reasonable. "
        "Use measured needs and feasible affordances only. "
        "Do not invent goals or actions. "
        "Return concise JSON only; do not narrate hidden chain-of-thought. "
        "Also provide brief user-facing telemetry: assessment, blocker, and next_step. "
        'Schema: {"goal_id":"exact candidate id","intention":"short purpose","reason":"one short evidence-grounded reason","assessment":"one sentence about the situation","blocker":"one short blocker or none","next_step":"one short next step"}.'
    )
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": instruction},
            {"role": "user", "content": json.dumps(world, separators=(",", ":"))},
        ],
        "options": {"temperature": 0.20},
        "keep_alive": "30m",
    }
    data, latency, metrics = ollama_chat_traced(payload, trace_path, timeout=180.0)
    parsed = json.loads(data.get("message", {}).get("content", "{}"))
    by_id = {g["goal_id"]: g for g in goals}
    proposed = str(parsed.get("goal_id", "")).strip()
    chosen = by_id.get(proposed)
    provenance = "qwen_plan"
    if chosen is None:
        chosen = choose_fallback_goal(goals)
        provenance = "planner_fallback"
    reason = str(parsed.get("reason", "")).strip()[:300]
    intention = str(parsed.get("intention", "")).strip()[:180]
    wm.last_planner_assessment = str(parsed.get("assessment", "")).strip()[:240]
    wm.last_planner_blocker = str(parsed.get("blocker", "")).strip()[:180]
    wm.last_planner_next_step = str(parsed.get("next_step", "")).strip()[:180]
    if intention:
        chosen = dict(chosen)
        chosen["intention"] = intention
    plan = synthesize_plan_from_goal(
        chosen, state, wm, actions,
        planner_reason=reason or "Qwen selected a feasible goal without a reason string",
        provenance=provenance,
    )
    return plan, latency, metrics, 1

def fallback_plan(state: dict, wm: WorldModel, actions: list[dict],
                  reason: str = "planner unavailable") -> Plan:
    goals = goal_candidates(state, actions, wm)
    goal = choose_fallback_goal(goals)
    return synthesize_plan_from_goal(
        goal, state, wm, actions,
        planner_reason=reason,
        provenance="planner_fallback",
    )

def safety_plan(state: dict, actions: list[dict], safety: tuple[dict, str]) -> Plan:
    action, reason = safety
    if action.get("action") == "drink_best":
        goal = {"goal_id": "reduce_thirst", "intention": "drink something safe to reduce urgent thirst"}
    elif action.get("action") == "eat_best_food":
        goal = {"goal_id": "reduce_hunger", "intention": "eat something safe to reduce urgent hunger"}
    else:
        goal = {"goal_id": "recover_stamina", "intention": "rest because stamina is critically low"}
    return synthesize_plan_from_goal(goal, state, WorldModel(), actions,
                                    planner_reason=reason, provenance="safety_plan")

def should_interrupt_plan(plan: Plan, state: dict, wm: WorldModel,
                          actions: list[dict]) -> str | None:
    step = plan.current_step
    if step is None:
        return "plan_complete"

    if plan.goal_id == "leave_shoreline" and not wm.shoreline_escape_active:
        return "shoreline_cleared"
    if plan.goal_id in {"search_for_shelter", "explore_frontier"} and state.get("strategic_landmarks"):
        return "strategic_landmark_spotted"
    if plan.goal_id == "approach_known_structure" and any(
        a.get("action") == "open_adjacent" for a in actions
    ):
        return "structure_boundary_reached"
    if plan.goal_id == "break_exploration_stall" and not wm.strategic_stall_active:
        return "strategic_stall_cleared"

    if plan.provenance == "fast_frontier" and needs_qwen_judgment(state, wm, actions):
        return "meaningful_judgment_event"

    safety = deterministic_safety(state, actions)
    if safety:
        safety_action = safety[0].get("action")
        expected = {
            "reduce_thirst": "drink_best",
            "reduce_hunger": "eat_best_food",
            "recover_stamina": "wait_one_turn",
        }.get(plan.goal_id)
        if safety_action != expected:
            return f"urgent_need_override:{safety_action}"

    hostiles = [
        c for c in state.get("nearby_creatures", [])
        if str(c.get("attitude", "")).lower() == "hostile"
        and max(abs(int(c.get("dx", 99) or 99)), abs(int(c.get("dy", 99) or 99))) <= 5
    ]
    if hostiles:
        return "hostile_within_5"

    if step.failure_count >= 2:
        return "current_step_failed_twice"

    if step.kind == "consume":
        if not any(a.get("action") == step.params.get("action") for a in actions):
            return "current_step_unsupported"
    if step.kind == "interact":
        action_name = step.params.get("action")
        item_name = step.params.get("item_name")
        if select_interaction(actions, action_name, item_name, step.target, state) is None:
            return "current_step_unsupported"
    return None

def consumable_counts(state: dict, predicate) -> dict[tuple[str, int, int], int]:
    counts: dict[tuple[str, int, int], int] = {}
    for item in state.get("inventory_consumables", []):
        if not predicate(item):
            continue
        key = (
            str(item.get("name", "")),
            int(item.get("nutrition", 0) or 0),
            int(item.get("quench", 0) or 0),
        )
        counts[key] = counts.get(key, 0) + 1
    return counts

def serializable_consumable_counts(counts: dict[tuple[str, int, int], int]) -> list[dict]:
    return [
        {"name": key[0], "nutrition": key[1], "quench": key[2], "count": count}
        for key, count in sorted(counts.items())
    ]

def inventory_consumable_changed(before: dict, after: dict, mode: str) -> tuple[bool, dict]:
    if mode == "eat":
        predicate = lambda x: int(x.get("nutrition", 0) or 0) > 0
    else:
        predicate = lambda x: int(x.get("quench", 0) or 0) > 0
    b = consumable_counts(before, predicate)
    a = consumable_counts(after, predicate)
    changed_keys = [key for key, count in b.items() if a.get(key, 0) < count]
    changed = bool(changed_keys)
    return changed, {
        "before": serializable_consumable_counts(b),
        "after": serializable_consumable_counts(a),
        "decreased_or_removed": [
            {"name": key[0], "nutrition": key[1], "quench": key[2]}
            for key in sorted(changed_keys)
        ],
    }

def verify_step(plan: Plan, action: dict, outcome: str, before: dict,
                after: dict, result: dict | None) -> tuple[bool, bool, dict]:
    step = plan.current_step
    if step is None:
        return True, True, {"evidence": "plan_already_complete"}

    if step.kind == "go_to":
        bx, by, bz = pos_tuple(before)
        ax, ay, az = pos_tuple(after)
        tx = int((step.target or {}).get("x", ax))
        ty = int((step.target or {}).get("y", ay))
        tz = int((step.target or {}).get("z", az))
        radius = int(step.params.get("arrival_radius", 0) or 0)
        before_d = max(abs(tx - bx), abs(ty - by)) if bz == tz else 10**9
        after_d = max(abs(tx - ax), abs(ty - ay)) if az == tz else 10**9
        progress = outcome == "moved" and after_d < before_d
        complete = after_d <= radius
        return progress, complete, {
            "evidence": "distance_to_target",
            "before_distance": before_d,
            "after_distance": after_d,
            "target": step.target,
        }

    if step.kind == "explore":
        moved = outcome == "moved" and pos_tuple(before) != pos_tuple(after)
        if moved:
            step.params["successful_moves"] = int(step.params.get("successful_moves", 0) or 0) + 1
        complete = int(step.params.get("successful_moves", 0) or 0) >= int(
            step.params.get("max_successful_moves", 6) or 6
        )
        return moved or outcome == "opened", complete, {
            "evidence": "explore_progress",
            "successful_moves": step.params.get("successful_moves", 0),
            "target_moves": step.params.get("max_successful_moves", 6),
        }

    if step.kind == "interact":
        action_name = step.params.get("action")
        if action_name == "pickup_consumable":
            verified = outcome == "pickup_verified"
            return verified, verified, {
                "evidence": "native_pickup_verified",
                "item_name": step.params.get("item_name"),
                "outcome": outcome,
            }
        if action_name == "open_adjacent":
            verified = outcome == "opened"
            return verified, verified, {"evidence": "native_opened", "outcome": outcome}
        if action_name == "wait_one_turn":
            verified = outcome == "waited"
            return verified, verified, {"evidence": "waited_once", "outcome": outcome}
        return False, False, {"evidence": "unsupported_interaction_verifier"}

    if step.kind == "consume":
        mode = "eat" if step.params.get("action") == "eat_best_food" else "drink"
        changed, inventory_evidence = inventory_consumable_changed(before, after, mode)
        activity_complete = str(after.get("activity", "") or "") == ""
        started = outcome == "consume_activity_started"
        verified = started and activity_complete and changed
        return verified, verified, {
            "evidence": "consume_activity_completed_and_inventory_changed",
            "mode": mode,
            "activity_started": started,
            "post_activity": activity_complete,
            "inventory_changed": changed,
            "inventory_evidence": inventory_evidence,
            "physiology": {
                "hunger_before": before.get("hunger"),
                "hunger_after": after.get("hunger"),
                "thirst_before": before.get("thirst"),
                "thirst_after": after.get("thirst"),
                "stored_kcal_before": before.get("stored_kcal"),
                "stored_kcal_after": after.get("stored_kcal"),
            },
        }

    if step.kind == "rest":
        b = int(before.get("stamina", 0) or 0)
        a = int(after.get("stamina", 0) or 0)
        maximum = max(1, int(after.get("stamina_max", 1) or 1))
        progress = outcome == "waited" and a > b
        complete = a / maximum >= float(step.params.get("until_ratio", 0.90))
        return progress, complete, {
            "evidence": "stamina_recovery",
            "before": b,
            "after": a,
            "target_ratio": step.params.get("until_ratio", 0.90),
        }

    return False, False, {"evidence": f"no_verifier_for_{step.kind}"}

def concise_action(choice: dict) -> str:
    return choice.get("label") or choice.get("action", "act")

def append_log(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

def command_kwargs(choice: dict) -> dict:
    return {k: choice[k] for k in ("dx", "dy", "item_name", "duration_minutes") if k in choice}

def compact_state_for_hash(state: dict) -> dict:
    return {
        "turn": state.get("turn"),
        "position": state.get("position"),
        "hunger": state.get("hunger"),
        "thirst": state.get("thirst"),
        "sleepiness": state.get("sleepiness"),
        "stamina": state.get("stamina"),
        "stamina_max": state.get("stamina_max"),
        "pain": state.get("pain"),
        "morale": state.get("morale"),
        "activity": state.get("activity"),
        "inventory_count": state.get("inventory_count"),
        "nearby_creatures": [
            {
                "kind": x.get("kind"),
                "name": x.get("name"),
                "dx": x.get("dx"),
                "dy": x.get("dy"),
                "attitude": x.get("attitude"),
            }
            for x in (state.get("nearby_creatures") or [])
        ],
    }

def state_hash(state: dict) -> str:
    raw = json.dumps(
        compact_state_for_hash(state), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]

def final_state_snapshot(state: dict) -> dict:
    return {
        "position": state.get("position"),
        "turn": state.get("turn"),
        "hunger": state.get("hunger"),
        "thirst": state.get("thirst"),
        "sleepiness": state.get("sleepiness"),
        "stamina": state.get("stamina"),
        "stamina_max": state.get("stamina_max"),
        "health": None,  # not yet exposed by the bridge; preserve schema slot
        "pain": state.get("pain"),
        "morale": state.get("morale"),
        "indoors": state.get("indoors"),
        "is_night": state.get("is_night"),
        "hostile_nearby": lesson_conditions(state).get("hostile_nearby"),
        "activity": state.get("activity"),
        "dead": state.get("dead"),
        "inventory_count": state.get("inventory_count"),
    }

def seconds_between_iso(started_at: str, ended_at: str) -> float | None:
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at)
        return max(0.0, (end - start).total_seconds())
    except Exception:
        return None

@dataclass
class LifeTelemetry:
    life_id: str
    life_number: int
    started_at: str
    started_turn: int
    last_actions: deque = field(default_factory=lambda: deque(maxlen=50))
    needs_history: list[dict] = field(default_factory=list)
    hostile_encounters: list[dict] = field(default_factory=list)
    resources_gained: dict = field(default_factory=lambda: {
        "items": [],
        "water_actions": 0,
        "food_actions": 0,
    })
    last_state: dict = field(default_factory=dict)
    last_needs_sample_action: int = -999999
    last_hostile_turn: int | None = None

    def observe(self, state: dict, action_count: int, force_sample: bool = False) -> None:
        self.last_state = final_state_snapshot(state)
        if force_sample or action_count - self.last_needs_sample_action >= 5:
            self.needs_history.append({
                "turn": state.get("turn"),
                "hunger": state.get("hunger"),
                "thirst": state.get("thirst"),
                "stamina": state.get("stamina"),
                "stamina_max": state.get("stamina_max"),
                "sleepiness": state.get("sleepiness"),
            })
            self.last_needs_sample_action = action_count
            if len(self.needs_history) > 200:
                self.needs_history = self.needs_history[-200:]

        hostiles = [
            x for x in (state.get("nearby_creatures") or [])
            if str(x.get("attitude", "")).lower() == "hostile"
        ]
        if hostiles:
            turn = int(state.get("turn", 0) or 0)
            if self.last_hostile_turn != turn:
                closest = min(
                    max(abs(int(x.get("dx", 99) or 99)), abs(int(x.get("dy", 99) or 99)))
                    for x in hostiles
                )
                self.hostile_encounters.append({
                    "turn": turn,
                    "count": len(hostiles),
                    "closest_distance": closest,
                    "outcome": "observed",
                })
                self.last_hostile_turn = turn
                if len(self.hostile_encounters) > 200:
                    self.hostile_encounters = self.hostile_encounters[-200:]

    def record_action(self, before: dict, after: dict, choice: dict,
                      outcome: str, verification: dict | None) -> None:
        self.last_actions.append({
            "turn": before.get("turn"),
            "action": choice.get("action"),
            "params": command_kwargs(choice),
            "outcome": outcome,
            "state_before_hash": state_hash(before),
            "state_after_hash": state_hash(after),
        })
        if verification and verification.get("evidence") == "native_pickup_verified":
            name = verification.get("item_name")
            if name:
                self.resources_gained["items"].append(str(name))
                self.resources_gained["items"] = self.resources_gained["items"][-100:]
        if verification and verification.get("evidence") == "consume_activity_completed_and_inventory_changed":
            if verification.get("mode") == "drink" and verification.get("inventory_changed"):
                self.resources_gained["water_actions"] += 1
            if verification.get("mode") == "eat" and verification.get("inventory_changed"):
                self.resources_gained["food_actions"] += 1

    def to_marker(self) -> dict:
        return {
            "schema_version": LIFE_RECORD_SCHEMA_VERSION,
            "life_id": self.life_id,
            "life_number": self.life_number,
            "started_at": self.started_at,
            "started_turn": self.started_turn,
            "last_actions": list(self.last_actions),
            "needs_history": self.needs_history,
            "hostile_encounters": self.hostile_encounters,
            "resources_gained": self.resources_gained,
            "last_state": self.last_state,
            "updated_at": utc_now(),
        }

    @classmethod
    def from_marker(cls, marker: dict) -> "LifeTelemetry":
        life = cls(
            life_id=str(marker.get("life_id") or uuid.uuid4().hex),
            life_number=int(marker.get("life_number", 1) or 1),
            started_at=str(marker.get("started_at") or utc_now()),
            started_turn=int(marker.get("started_turn", 0) or 0),
        )
        life.last_actions = deque(marker.get("last_actions") or [], maxlen=50)
        life.needs_history = list(marker.get("needs_history") or [])
        life.hostile_encounters = list(marker.get("hostile_encounters") or [])
        life.resources_gained = dict(marker.get("resources_gained") or {
            "items": [], "water_actions": 0, "food_actions": 0
        })
        life.last_state = dict(marker.get("last_state") or {})
        return life

@dataclass
class LifeRecord:
    life_id: str
    life_number: int
    terminal_state: str
    started_at: str
    ended_at: str
    duration_seconds: float | None
    duration_game_turns: int | None
    final_state: dict
    death_cause: dict | None
    abort_reason: str | None
    active_plan_at_end: dict | None
    last_actions: list[dict]
    needs_history: list[dict]
    hostile_encounters: list[dict]
    resources_gained: dict

    def to_dict(self) -> dict:
        return {
            "schema_version": LIFE_RECORD_SCHEMA_VERSION,
            "event": "life_terminal",
            "life_id": self.life_id,
            "life_number": self.life_number,
            "terminal_state": self.terminal_state,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "duration_game_turns": self.duration_game_turns,
            "final_state": self.final_state,
            "death_cause": self.death_cause,
            "abort_reason": self.abort_reason,
            "active_plan_at_end": self.active_plan_at_end,
            "last_actions": self.last_actions,
            "needs_history": self.needs_history,
            "hostile_encounters": self.hostile_encounters,
            "resources_gained": self.resources_gained,
        }

def write_active_life_marker(life: LifeTelemetry) -> None:
    write_json_atomic(ACTIVE_LIFE_PATH, life.to_marker())

def clear_active_life_marker() -> None:
    try:
        ACTIVE_LIFE_PATH.unlink()
    except OSError:
        pass

def recover_user_interrupted_life(evolution_state: dict, state: dict) -> LifeTelemetry | None:
    """Restore the same life identity after an older Ctrl+C stop was logged as aborted."""
    if read_json_safe(ACTIVE_LIFE_PATH):
        return None
    if evolution_state.get("current_life_id"):
        return None
    if bool(state.get("dead")):
        return None

    records = load_recent_life_history(limit=1, include_aborted=True)
    if not records:
        return None
    record = records[-1]
    if record.get("terminal_state") != "aborted" or record.get("abort_reason") != "user_interrupt":
        return None

    life_id = str(record.get("life_id") or "").strip()
    if not life_id:
        return None
    life_number = int(record.get("life_number", 1) or 1)
    started_at = str(record.get("started_at") or utc_now())
    final_state = record.get("final_state") or {}
    final_turn = final_state.get("turn")
    duration_turns = record.get("duration_game_turns")
    try:
        if final_turn is not None and duration_turns is not None:
            started_turn = int(final_turn) - int(duration_turns)
        else:
            started_turn = int(state.get("turn", 0) or 0)
    except Exception:
        started_turn = int(state.get("turn", 0) or 0)

    life = LifeTelemetry(
        life_id=life_id,
        life_number=life_number,
        started_at=started_at,
        started_turn=started_turn,
    )
    life.last_actions = deque(record.get("last_actions") or [], maxlen=50)
    life.needs_history = list(record.get("needs_history") or [])
    life.hostile_encounters = list(record.get("hostile_encounters") or [])
    life.resources_gained = dict(record.get("resources_gained") or {
        "items": [], "water_actions": 0, "food_actions": 0
    })
    life.observe(state, len(life.last_actions), force_sample=True)

    evolution_state["current_life_id"] = life_id
    evolution_state["current_life_started_at"] = started_at
    evolution_state["current_life_started_turn"] = started_turn
    evolution_state["next_life_number"] = life_number
    evolution_state["life_number"] = life_number
    save_evolution_state(evolution_state)
    write_active_life_marker(life)
    return life

def ensure_current_life(evolution_state: dict, state: dict) -> LifeTelemetry:
    marker = read_json_safe(ACTIVE_LIFE_PATH)
    current_id = evolution_state.get("current_life_id")
    next_number = int(
        evolution_state.get("next_life_number", evolution_state.get("life_number", 1)) or 1
    )

    if (
        marker
        and int(marker.get("schema_version", -1)) == LIFE_RECORD_SCHEMA_VERSION
        and current_id
        and str(marker.get("life_id")) == str(current_id)
        and int(marker.get("life_number", -1)) == next_number
    ):
        life = LifeTelemetry.from_marker(marker)
        life.observe(state, len(life.last_actions), force_sample=True)
        write_active_life_marker(life)
        return life

    life_id = uuid.uuid4().hex
    started_at = utc_now()
    started_turn = int(state.get("turn", 0) or 0)
    evolution_state["current_life_id"] = life_id
    evolution_state["current_life_started_at"] = started_at
    evolution_state["current_life_started_turn"] = started_turn
    evolution_state["next_life_number"] = next_number
    evolution_state["life_number"] = next_number
    save_evolution_state(evolution_state)

    life = LifeTelemetry(
        life_id=life_id,
        life_number=next_number,
        started_at=started_at,
        started_turn=started_turn,
    )
    life.observe(state, 0, force_sample=True)
    write_active_life_marker(life)
    return life

def make_life_record(life: LifeTelemetry, terminal_state: str,
                     final_state: dict, plan: Plan | None,
                     death_status: dict | None = None,
                     abort_reason: str | None = None) -> LifeRecord:
    ended_at = utc_now()
    final_turn = final_state.get("turn")
    duration_turns = None
    try:
        if final_turn is not None:
            duration_turns = max(0, int(final_turn) - int(life.started_turn))
    except Exception:
        duration_turns = None
    death_cause = None
    if terminal_state == "dead":
        death_cause = {
            "type": "game_over",
            "evidence": death_status,
            "specific_cause": None,
        }
    return LifeRecord(
        life_id=life.life_id,
        life_number=life.life_number,
        terminal_state=terminal_state,
        started_at=life.started_at,
        ended_at=ended_at,
        duration_seconds=seconds_between_iso(life.started_at, ended_at),
        duration_game_turns=duration_turns,
        final_state=final_state_snapshot(final_state),
        death_cause=death_cause,
        abort_reason=abort_reason,
        active_plan_at_end=plan.to_dict() if plan else None,
        last_actions=list(life.last_actions),
        needs_history=list(life.needs_history),
        hostile_encounters=list(life.hostile_encounters),
        resources_gained=dict(life.resources_gained),
    )

def record_life_terminal(evolution_state: dict, life: LifeTelemetry,
                         terminal_state: str, final_state: dict,
                         plan: Plan | None, log_path: Path,
                         death_status: dict | None = None,
                         abort_reason: str | None = None) -> LifeRecord:
    life.observe(final_state, len(life.last_actions), force_sample=True)
    record = make_life_record(
        life, terminal_state, final_state, plan,
        death_status=death_status, abort_reason=abort_reason
    )
    payload = record.to_dict()
    append_life_history(payload)
    append_log(log_path, payload)

    if terminal_state == "dead":
        lesson = make_death_lesson(life, final_state)
        if lesson and append_lesson(lesson):
            append_log(log_path, {
                "wall_time": utc_now(),
                "session_event": "lesson_created",
                "life_id": life.life_id,
                "life_number": life.life_number,
                "lesson": lesson,
            })
        evolution_state["lives_completed"] = int(evolution_state.get("lives_completed", 0)) + 1
        evolution_state["next_life_number"] = life.life_number + 1
        evolution_state["life_number"] = life.life_number + 1
        # Authoritative duplicate-death guard. The consumed-status file is a
        # cache/diagnostic only and is never authoritative.
        evolution_state["last_consumed_death_signature"] = life_status_signature(death_status)
        evolution_state["last_life_end"] = {
            "life_id": life.life_id,
            "ended_at": record.ended_at,
            "duration_seconds": record.duration_seconds,
            "duration_game_turns": record.duration_game_turns,
            "final_state": record.final_state,
        }
    else:
        evolution_state["next_life_number"] = life.life_number
        evolution_state["life_number"] = life.life_number

    evolution_state.pop("current_life_id", None)
    evolution_state.pop("current_life_started_at", None)
    evolution_state.pop("current_life_started_turn", None)

    # Evolution state is durable truth. Only after it is saved do we clear the
    # active marker/cache for this terminal attempt.
    save_evolution_state(evolution_state)
    clear_active_life_marker()
    return record

def recover_previous_runtime(evolution_state: dict, log_path: Path) -> bool:
    status = read_life_status()
    dead_signature = life_status_signature(status)
    last_consumed = evolution_state.get("last_consumed_death_signature")

    if not status or str(status.get("status", "")) != "dead":
        return False

    if dead_signature and dead_signature == last_consumed:
        archive_consumed_life_status(status)
        return True

    marker = read_json_safe(ACTIVE_LIFE_PATH)
    if marker and int(marker.get("schema_version", -1)) == LIFE_RECORD_SCHEMA_VERSION:
        life = LifeTelemetry.from_marker(marker)
        final_state = dict(marker.get("last_state") or {})
    else:
        life = LifeTelemetry(
            life_id=str(evolution_state.get("current_life_id") or uuid.uuid4().hex),
            life_number=int(
                evolution_state.get(
                    "next_life_number", evolution_state.get("life_number", 1)
                ) or 1
            ),
            started_at=str(evolution_state.get("current_life_started_at") or utc_now()),
            started_turn=int(
                evolution_state.get(
                    "current_life_started_turn", status.get("turn", 0)
                ) or 0
            ),
        )
        final_state = {
            "turn": status.get("turn"),
            "position": status.get("position"),
            "activity": status.get("activity"),
            "dead": True,
        }

    record_life_terminal(
        evolution_state, life, "dead", final_state, None, log_path,
        death_status=status
    )
    # Evolution state was saved first; this cache/rotation may safely lag.
    archive_consumed_life_status(status)
    return True

def wait_for_manual_respawn() -> dict:
    print()
    print("NOVA LIFE ENDED")
    print("Manual respawn validation: create/load a NEW character in the SAME CDDA world.")
    print("Nova will wait here and automatically reattach when the new character reaches the map.")
    print()
    last_notice = 0.0
    while True:
        status = read_life_status()
        if status and str(status.get("status", "")) == "alive":
            try:
                obs = send_command("observe", timeout=30.0)
                state = state_from_response(obs)
                if not bool(state.get("dead")):
                    return state
            except LifeEnded:
                pass
            except Exception:
                pass
        now = time.monotonic()
        if now - last_notice >= 15.0:
            print("...waiting for next living character in the same world")
            last_notice = now
        time.sleep(0.25)

def log_life_started(log_path: Path, life: LifeTelemetry, resumed: bool) -> None:
    previous = previous_life_context(5)
    append_log(log_path, {
        "wall_time": utc_now(),
        "session_event": "life_started",
        "life_id": life.life_id,
        "life_number": life.life_number,
        "resumed_existing_attempt": resumed,
        "previous_lives": previous,
    })
    print(
        f"NOVA LIFE {life.life_number} STARTED — life_id={life.life_id[:8]} "
        f"| previous dead lives: {len(previous)}"
    )

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
    dashboard = DashboardFeed()
    evolution_state = load_evolution_state()
    must_wait_for_respawn = recover_previous_runtime(evolution_state, log_path)
    life_number = int(
        evolution_state.get("next_life_number", evolution_state.get("life_number", 1)) or 1
    )

    print("NOVA EVOLUTION — PLANNER + EXECUTOR + LIFE LOOP")
    if RUN_MINUTES <= 0:
        print("Run target: until real death, transport failure, or manual stop")
    else:
        print(f"Run target: {RUN_MINUTES} minutes")
    print(f"Ollama model: {model or 'NOT FOUND - deterministic planner fallback'}")
    print(f"Log: {log_path}")
    print(f"Model trace: {model_trace_path}")
    print(f"Life: {life_number} | completed lives: {evolution_state.get('lives_completed', 0)}")
    print()

    feed.push("NOVA: waiting for a character to enter the world.")
    print("Waiting for a loaded character. You can take your time in the menus...")

    try:
        if must_wait_for_respawn:
            state = wait_for_manual_respawn()
        else:
            obs = send_command("observe", timeout=1800.0)
            state = state_from_response(obs)
    except LifeEnded as ended:
        # An unconsumed death raced startup after recovery; preserve it once.
        temp_life = ensure_current_life(evolution_state, {
            "turn": ended.status.get("turn"),
            "position": ended.status.get("position"),
            "dead": True,
        })
        record_life_terminal(
            evolution_state, temp_life, "dead", temp_life.last_state, None, log_path,
            death_status=ended.status
        )
        archive_consumed_life_status(ended.status)
        state = wait_for_manual_respawn()
    except Exception as exc:
        # Startup transport failure is not a life event. If an active marker
        # exists, leave it untouched so a later startup can ask CDDA whether
        # the same body is still alive and resume the same life_id.
        marker = read_json_safe(ACTIVE_LIFE_PATH)
        append_log(log_path, {
            "wall_time": utc_now(),
            "session_event": "transport_failure",
            "stage": "startup_observe",
            "error": repr(exc),
            "life_id": marker.get("life_id") if marker else evolution_state.get("current_life_id"),
            "life_number": marker.get("life_number") if marker else evolution_state.get("next_life_number"),
            "terminal_record_written": False,
            "active_marker_preserved": bool(marker),
        })
        print(f"Cannot reach CDDA bridge: {exc}")
        if marker:
            print(
                "TRANSPORT FAILURE — existing life identity preserved for recovery: "
                f"{str(marker.get('life_id', ''))[:8]}"
            )
        feed.push("ERROR: cannot reach CDDA bridge; life identity preserved.")
        return 4

    wm.observe(state)
    lessons = load_lessons()

    if MODEL_AUDIT:
        actions = available_actions(state, wm)
        actions, priority_context = apply_priority_ladder(state, actions)
        actions, _ = apply_lesson_bias(state, actions, lessons)
        wm.priority_context = priority_context
        print()
        print("MODEL AUDIT: sending two real planning requests to Ollama...")
        try:
            first = qwen_plan(model, state, wm, actions, model_trace_path)
            plan1, latency1, metrics1, raw_count1 = first
            time.sleep(1.0)
            second = qwen_plan(model, state, wm, actions, model_trace_path)
            plan2, latency2, metrics2, raw_count2 = second
        except Exception as exc:
            print(f"MODEL AUDIT FAILED: {exc!r}")
            print(f"Trace: {model_trace_path}")
            return 3

        print("MODEL AUDIT PASSED")
        print(f"Endpoint: {OLLAMA}/api/chat")
        print(f"Model: {model}")
        print("keep_alive: 30m")
        print()
        print(f"CALL 1 latency: {latency1:.3f}s | Ollama load: {metrics1.get('load_duration_seconds')}s")
        print(f"CALL 1 goal_id: {plan1.goal_id} | steps: {[s.kind for s in plan1.steps]}")
        print()
        print(f"CALL 2 latency: {latency2:.3f}s | Ollama load: {metrics2.get('load_duration_seconds')}s")
        print(f"CALL 2 goal_id: {plan2.goal_id} | steps: {[s.kind for s in plan2.steps]}")
        print()
        print(f"Raw request/response trace: {model_trace_path}")
        print("No game action was dispatched in audit mode.")
        return 0

    marker_before_life = read_json_safe(ACTIVE_LIFE_PATH)
    recovered_interrupt_life = recover_user_interrupted_life(evolution_state, state)
    life = recovered_interrupt_life or ensure_current_life(evolution_state, state)
    resumed_existing_attempt = bool(
        recovered_interrupt_life
        or (
            marker_before_life
            and str(marker_before_life.get("life_id")) == life.life_id
        )
    )
    life_number = life.life_number
    log_life_started(log_path, life, resumed_existing_attempt)
    deadline = None if RUN_MINUTES <= 0 else time.monotonic() + RUN_MINUTES * 60
    action_count = 0
    plan: Plan | None = None

    abort_reason = "validation_window_ended"
    transport_failure: dict | None = None
    try:
        while deadline is None or time.monotonic() < deadline:
            sit = situation_summary(state, wm)
            actions = available_actions(state, wm)
            actions, priority_context = apply_priority_ladder(state, actions)
            actions, matched_lessons = apply_lesson_bias(state, actions, lessons)
            wm.priority_context = priority_context
            safety = deterministic_safety(state, actions)

            lesson_signature = "|".join(
                sorted(str(m.get("lesson_id")) for m in matched_lessons if m.get("lesson_id"))
            )
            if lesson_signature != wm.last_lesson_signature:
                wm.last_lesson_signature = lesson_signature
                if matched_lessons:
                    first = matched_lessons[0]
                    memory_line = (
                        f"MEMORY: biasing away from {first.get('at_death_action')} — "
                        "similar conditions killed me before."
                    )
                    feed.push(memory_line)
                    append_log(log_path, {
                        "wall_time": utc_now(),
                        "session_event": "lesson_fired",
                        "life_id": life.life_id,
                        "life_number": life.life_number,
                        "priority_context": priority_context,
                        "matches": matched_lessons,
                        "current_conditions": lesson_conditions(state),
                    })

            feed.push("SEE: " + describe_situation(sit))
            dashboard.update(state, wm, plan, matched_lessons)
    
            if plan and plan.completed:
                append_log(log_path, {
                    "wall_time": utc_now(),
                    "plan_event": "completed",
                    "plan": plan.to_dict(),
                    "position": state.get("position"),
                })
                feed.push("PLAN: completed " + plan.goal_id)
                plan = None
                wm.active_goal_id = ""
                wm.active_intention = ""
                continue
    
            if plan:
                interrupt_reason = should_interrupt_plan(plan, state, wm, actions)
                if interrupt_reason:
                    append_log(log_path, {
                        "wall_time": utc_now(),
                        "plan_event": "interrupted",
                        "reason": interrupt_reason,
                        "plan": plan.to_dict(),
                        "position": state.get("position"),
                    })
                    feed.push("PLAN: interrupted — " + interrupt_reason)
                    plan = None
                    wm.active_goal_id = ""
                    wm.active_intention = ""
                    continue
    
            if plan is None:
                model_error = None
                model_latency_seconds = None
                model_metrics = None
    
                if safety:
                    plan = safety_plan(state, actions, safety)
                elif not needs_qwen_judgment(state, wm, actions):
                    plan = fast_frontier_plan(state, actions)
                elif model:
                    try:
                        plan, model_latency_seconds, model_metrics, _ = qwen_plan(
                            model, state, wm, actions, model_trace_path
                        )
                    except Exception as exc:
                        model_error = repr(exc)
                        plan = fallback_plan(state, wm, actions, reason=f"planner error: {model_error}")
                else:
                    plan = fallback_plan(state, wm, actions)
    
                wm.active_goal_id = plan.goal_id
                wm.active_intention = plan.intention
                append_log(log_path, {
                    "wall_time": utc_now(),
                    "plan_event": "created",
                    "plan": plan.to_dict(),
                    "model": model,
                    "model_error": model_error,
                    "model_latency_seconds": (
                        round(model_latency_seconds, 3) if model_latency_seconds is not None else None
                    ),
                    "model_metrics": model_metrics,
                    "position": state.get("position"),
                })
                feed.push("INTENT: " + plan.intention)
                dashboard.update(state, wm, plan, matched_lessons)
    
            # A step can already be complete when the previous step placed Nova at
            # its completion boundary. Advance without spending a game action.
            while plan and plan.current_step and plan_step_complete_before_action(plan.current_step, state):
                completed_step = plan.current_step.to_dict()
                plan.advance()
                append_log(log_path, {
                    "wall_time": utc_now(),
                    "plan_event": "step_completed_without_dispatch",
                    "completed_step": completed_step,
                    "plan": plan.to_dict(),
                    "position": state.get("position"),
                })
                if plan.completed:
                    break
    
            if plan is None or plan.completed:
                continue
    
            step = plan.current_step
            choice, executor_reason = execute_step(plan, state, wm, actions)
            if not choice:
                step.failure_count += 1
                step.last_failure_reason = executor_reason
                append_log(log_path, {
                    "wall_time": utc_now(),
                    "plan_event": "step_no_action",
                    "reason": executor_reason,
                    "plan": plan.to_dict(),
                    "state": state,
                })
                feed.push("PLAN: step blocked — " + executor_reason)
                wm.last_planner_blocker = executor_reason
                dashboard.update(state, wm, plan, matched_lessons, blocker=executor_reason)
                if step.failure_count >= 2:
                    if plan.goal_id == "approach_known_structure" and step.target:
                        try:
                            failed_key = (
                                int(step.target.get("x")),
                                int(step.target.get("y")),
                                int(step.target.get("z")),
                            )
                            wm.failed_landmarks.add(failed_key)
                        except Exception:
                            pass
                    plan = None
                    wm.active_goal_id = ""
                    wm.active_intention = ""
                continue
    
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
                    "plan": plan.to_dict(),
                })
                feed.push("BLOCKED: excluded validation action " + str(action))
                print(command_error)
                break
    
            feed.push(f"DO: {concise_action(choice)} — {executor_reason}")
            state_before = state
            started = time.monotonic()
            try:
                result = send_command(action, timeout=60.0, **command_kwargs(choice))
                command_error = None
            except LifeEnded as ended:
                death_record = record_life_terminal(
                    evolution_state, life, "dead", state_before, plan, log_path,
                    death_status=ended.status
                )
                archive_consumed_life_status(ended.status)
                print(f"LIFE {life.life_number} ended after {death_record.duration_seconds}s.")
                lessons = load_lessons()
                state = wait_for_manual_respawn()
                wm = WorldModel()
                wm.observe(state)
                plan = None
                life = ensure_current_life(evolution_state, state)
                life_number = life.life_number
                log_life_started(log_path, life, False)
                continue
            except Exception as exc:
                result = None
                command_error = repr(exc)
    
            action_count += 1
            outcome = result.get("outcome") if result else "command_error"
            bridge_latency = round(time.monotonic() - started, 3)
            wm.last_action_summary = f"{action} -> {outcome}"
    
            if command_error:
                append_log(log_path, {
                    "wall_time": utc_now(),
                    "action_index": action_count,
                    "command_error": command_error,
                    "selected": choice,
                    "plan": plan.to_dict(),
                    "state_before": state_before,
                    "bridge_latency_seconds": bridge_latency,
                })
                print(f"[{action_count}] {action}: COMMAND ERROR {command_error}")
                transport_failure = {
                    "stage": "command",
                    "action": action,
                    "error": command_error,
                }
                break
    
            wm.record_action(state_before, choice, outcome, result)
            dashboard.update(state, wm, plan, matched_lessons)
    
            # Option A activity contract: with the native C++ gate, this observe
            # should not be serviced until the current activity has completed.
            post_activity_observe_retries = 0
            try:
                obs = send_command("observe", timeout=900.0)
                state = state_from_response(obs)
                if action in {"eat_best_food", "drink_best"} and str(state.get("activity", "") or ""):
                    post_activity_observe_retries = 1
                    obs = send_command("observe", timeout=900.0)
                    state = state_from_response(obs)
                wm.observe(state)
            except LifeEnded as ended:
                death_record = record_life_terminal(
                    evolution_state, life, "dead", state_before, plan, log_path,
                    death_status=ended.status
                )
                archive_consumed_life_status(ended.status)
                print(f"LIFE {life.life_number} ended after {death_record.duration_seconds}s.")
                lessons = load_lessons()
                state = wait_for_manual_respawn()
                wm = WorldModel()
                wm.observe(state)
                plan = None
                life = ensure_current_life(evolution_state, state)
                life_number = life.life_number
                log_life_started(log_path, life, False)
                continue
            except Exception as exc:
                append_log(log_path, {"wall_time": utc_now(), "observe_error": repr(exc)})
                feed.push("ERROR: lost world observation.")
                print(f"Observe failed: {exc}")
                transport_failure = {
                    "stage": "observe",
                    "action": action,
                    "error": repr(exc),
                }
                break
    
            progress, step_complete, verification = verify_step(
                plan, choice, outcome, state_before, state, result
            )
            verification["post_activity_contract"] = (
                str(state.get("activity", "") or "") == ""
                if action in {"eat_best_food", "drink_best"}
                else None
            )
            verification["post_activity_observe_retries"] = post_activity_observe_retries
    
            step = plan.current_step
            if progress:
                step.failure_count = 0
                step.last_failure_reason = ""
            else:
                step.failure_count += 1
                step.last_failure_reason = str(verification.get("evidence", outcome))
    
            if step_complete:
                completed_step = step.to_dict()
                plan.advance()
                plan_event = "step_completed"
            else:
                completed_step = None
                plan_event = "step_progress" if progress else "step_failed"
    
            life.record_action(state_before, state, choice, outcome, verification)
            life.observe(state, action_count)
            write_active_life_marker(life)
    
            append_log(log_path, {
                "wall_time": utc_now(),
                "action_index": action_count,
                "life_id": life.life_id,
                "life_number": life.life_number,
                "decision_mode": "plan_executor",
                "selected_provenance": choice.get("provenance", "plan_executor"),
                "selected": choice,
                "reason": executor_reason,
                "active_goal_id": plan.goal_id,
                "active_intention": plan.intention,
                "plan_event": plan_event,
                "plan": plan.to_dict(),
                "completed_step": completed_step,
                "state_before": state_before,
                "result": result,
                "state_after": state,
                "verification": verification,
                "bridge_latency_seconds": bridge_latency,
                "world_model": {
                    "visited_positions": len(wm.visits),
                    "known_tiles": len(wm.known_tiles),
                    "loop_detected": wm.looping(),
                    "known_blocked_edges": len(wm.blocked_edges),
            "known_hazard_tiles": len(wm.hazard_tiles),
            "stall_escape_active": bool(wm.stall_avoid_tiles),
            "shoreline_escape_active": wm.shoreline_escape_active,
            "current_water_distance": wm.nearest_known_water_distance(pos_tuple(state)),
            "strategic_stall_active": wm.strategic_stall_active,
            "strategic_progress": strategic_progress_summary(wm),
            "known_structure_targets": known_structure_targets(state, wm)[:5],
                },
            })
    
            feed.push(("VERIFIED: " if step_complete else "RESULT: ") + f"{action} — {verification.get('evidence')}")
            print(
                f"[{action_count}] {action}: {outcome} | mode=plan_executor | "
                f"goal={plan.goal_id!r} | step={step.kind!r} | "
                f"{verification.get('evidence')}"
            )
    
            if step.failure_count >= 2:
                append_log(log_path, {
                    "wall_time": utc_now(),
                    "plan_event": "step_failure_limit",
                    "plan": plan.to_dict(),
                    "position": state.get("position"),
                })
                feed.push("PLAN: abandoned after repeated step failure.")
                plan = None
                wm.active_goal_id = ""
                wm.active_intention = ""
    
    except KeyboardInterrupt:
        abort_reason = "user_interrupt"
        life.observe(state, action_count, force_sample=True)
        write_active_life_marker(life)
        append_log(log_path, {
            "wall_time": utc_now(),
            "session_event": "manual_stop",
            "life_id": life.life_id,
            "life_number": life.life_number,
            "terminal_record_written": False,
            "active_marker_preserved": True,
        })
        feed.push("SESSION: manually stopped; life identity preserved.")
        print()
        print("Nova runtime stopped by user; current life identity was preserved.")
        print(f"Life {life.life_number} / {life.life_id[:8]} can resume later.")
        print(f"Log saved to: {log_path}")
        return 130

    if transport_failure is not None:
        # Transport/runtime failure is not a life event. Preserve the active
        # marker and current life identity so the next startup can ask CDDA
        # whether this same body is still alive.
        life.observe(state, action_count, force_sample=True)
        write_active_life_marker(life)
        append_log(log_path, {
            "wall_time": utc_now(),
            "session_event": "transport_failure",
            "life_id": life.life_id,
            "life_number": life.life_number,
            "transport_failure": transport_failure,
            "terminal_record_written": False,
        })
        feed.push("ERROR: bridge transport failed; life identity preserved.")
        print()
        print("TRANSPORT FAILURE — life was NOT ended.")
        print(f"Life {life.life_number} / {life.life_id[:8]} remains active for recovery.")
        print(f"Log saved to: {log_path}")
        return 4

    # A validation window ending is not a death. Record it as aborted/debug.
    # The next attempt keeps the same life_number but receives a fresh UUID.
    try:
        status = read_life_status()
        if not status or str(status.get("status", "")) != "dead":
            record_life_terminal(
                evolution_state, life, "aborted", state, plan, log_path,
                abort_reason=abort_reason
            )
    except Exception as exc:
        append_log(log_path, {"wall_time": utc_now(), "abort_record_error": repr(exc)})

    feed.push(f"SESSION: finished after {action_count} actions.")
    print()
    print(f"Run finished after {action_count} actions.")
    print(f"Log saved to: {log_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
