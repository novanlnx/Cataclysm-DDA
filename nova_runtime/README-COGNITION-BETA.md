# Nova Cognition + UI Beta

This milestone adds a first executive cognition layer on top of the proven CDDA survival bridge.

Key changes:
- persistent local world model with visit counts
- anti-loop / immediate-backtrack penalties
- frontier and closed-door progress scoring
- native indoor/outdoor context
- persistent short-horizon intention
- Qwen + grounded controller hybrid scoring
- controlled variation only among near-tied coherent choices
- sleep hidden unless the avatar is actually tired
- three-line in-game Nova panel sourced from real situation/intention/action/outcome events
- no hidden chain-of-thought is displayed

The Nova panel reads NOVA_BRIDGE_DIR/nova-thoughts.txt and always shows the latest three concise entries.
