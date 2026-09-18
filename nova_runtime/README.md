# Nova CDDA Survival Alpha Runtime

This is the first batch-oriented runtime after the OBSERVE + MOVE spike.

Native bridge vocabulary:
- observe
- move_one_tile
- wait_one_turn
- open_adjacent
- eat_best_food
- drink_best
- sleep
- quicksave

The Python runner:
- uses deterministic safety for urgent thirst/hunger/sleepiness/stamina,
- asks local Ollama/Qwen for ordinary choices,
- validates every choice against currently available actions,
- uses weighted variation among near-tied choices,
- falls back to conservative movement/waiting if Ollama fails,
- logs every state, decision and result to JSONL,
- quicksaves every 25 actions.

This is intentionally an alpha data-gathering runtime, not the finished Generative-Agents cognition layer.
