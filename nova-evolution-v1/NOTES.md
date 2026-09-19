# Nova Evolution v1 — Lifecycle Notes

This file defines the lifecycle rules for the first reincarnation proof. Do not add learning, reflection, automated respawn, menu automation, or new prompt behavior until the two-life test below passes.

## Schema versions

- Evolution state: `EVOLUTION_STATE_VERSION = 1`
- Life record ledger: `LIFE_RECORD_SCHEMA_VERSION = 1`
- Bridge life-status file: `LIFE_STATUS_SCHEMA_VERSION = 1`

Persistent files:

- `nova-state/nova-evolution-state-v1.json`
- `nova-state/nova-active-life-v1.json`
- `nova-state/nova-life-history-v1.jsonl`
- `nova-state/nova-consumed-life-status-v1.json`
- `nova-ipc/life-status.json`

## Identity rules

- `life_number` is the reincarnation number.
- `life_id` is always a fresh `uuid4().hex` for each attempt.
- An aborted attempt does **not** advance `life_number`.
- The next attempt after an abort keeps the same `life_number` but gets a new `life_id`.
- A real death advances `next_life_number` by one.

## Authoritative-state rule

`nova-evolution-state-v1.json` is authoritative.

`nova-consumed-life-status-v1.json` is only a diagnostic/cache copy of the last consumed bridge status. It must never be used by itself to decide that a death was already processed.

For a real death the order is:

1. append the life terminal record;
2. update and atomically save evolution state, including `last_consumed_death_signature` and `next_life_number`;
3. clear the active-life marker;
4. archive/remove the bridge `life-status.json`.

A crash after step 2 may leave stale cache files, but the evolution state still decides whether the death counted.

## Stale active-life startup rule

If `nova-active-life-v1.json` exists on startup:

- observe succeeds and the character is alive -> resume the existing attempt and keep its `life_id`;
- bridge/status reports dead -> record exactly one dead terminal record, advance life number, and wait for manual respawn;
- startup observe fails or times out -> record that attempt as `aborted`, do not increment life number, and do not consume a death signature.

A clean Ctrl+C is also recorded as `aborted`.

## Qwen inheritance invariant

Qwen must **never** receive raw previous-life events.

Raw life records are evidence for future reflection. Reflection will eventually produce compact summaries/lessons. Only those future summaries may be retrieved into Qwen planning prompts.

For the two-life test, the runtime log emits a `session_event: "life_started"` record with a `previous_lives` list containing only lifecycle metadata (life id/number, terminal state, duration, end time). This is test/session context and is not included in Qwen input.

## Life record minimum shape

Every terminal record uses `event: "life_terminal"` and includes:

- `schema_version`
- `life_id`
- `life_number`
- `terminal_state`: `dead` or `aborted`
- `started_at`, `ended_at`, `duration_seconds`, `duration_game_turns`
- `final_state`: position, turn, hunger, thirst, sleepiness, stamina, health slot, pain, morale, indoors, activity, dead, inventory count
- `death_cause` or `abort_reason`
- `active_plan_at_end`
- up to 50 recent actions with params, outcome, before-state hash, after-state hash
- sampled needs history
- hostile encounter history
- resources gained

## Two-life test pass criteria

Pass requires all of the following:

1. Deliberately kill Life 1.
2. Python detects the bridge `dead` status without relying on a timeout.
3. Ledger contains exactly one `terminal_state: "dead"` record for Life 1.
4. Evolution state shows `next_life_number: 2`.
5. Manually create a new character in the **same CDDA world**.
6. When the new character reaches the map, the same Python runtime reconnects.
7. The session-start log shows `life_number: 2`.
8. Life 2 session-start context contains `previous_lives` with exactly one entry.
9. Kill Life 2.
10. Evolution state shows `next_life_number: 3`, and Life 1 remains present in the ledger.

Fail if any of these occur:

- Life 1 is recorded twice.
- Life 2 starts with the wrong number.
- Life 1 disappears after restart/respawn.
- A normal death also produces a spurious aborted record.

## Separate abort test

End a living run with Ctrl+C.

Expected:

- one `terminal_state: "aborted"` record;
- no increment to `next_life_number`;
- no new death signature consumed;
- the next attempt has the same `life_number` but a different `life_id`.

## Inspection CLI

Run:

`py -3 nova_runtime/nova_life_log.py`

It prints one line per terminal record and the current next-life counter. It is expected to work against an empty ledger.

## Explicitly deferred

Do not build these until the two-life test passes:

- Q-table / learned values
- reward function
- death reflection
- automated same-world respawn
- menu automation
- additional prompt inheritance
- combat/crafting/building expansion
