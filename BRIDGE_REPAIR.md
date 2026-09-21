# Bridge protocol repair

Bridge `2026.09.20.4` preserves declared tool namespaces and emits complete
reasoning SSE items. DeepSeek protocol mode also keeps an 8 MiB, one-hour,
memory-only cache of exact upstream reasoning items. The cache is scoped by
authentication/model and can recover history by either the reasoning ID or the
following call/message ID. The latter covers clients that omit the complete
reasoning item on a later tool round.

Recovery is deliberately conservative. Existing reasoning text is never
replaced, conflicting anchors are invalidated, and an unmatched or ambiguous
request passes through unchanged. The bridge never invents reasoning text and
does not persist cached reasoning. Compact requests without an exact match stay
byte-for-byte passthrough. Optional raw capture remains separate and off by
default.

Metadata records only reasoning item counts and a boolean for the specific
upstream `reasoning_text ... passed back` error. It does not record reasoning
text, prompts, tool arguments, authorization headers, or API keys.

Focused regression coverage includes omitted reasoning items, ID-less stubs,
owner isolation, conflicting anchors, cache expiry, long tool sequences, and a
two-request HTTP/SSE replay. An isolated real DeepSeek task also completed a
tool round and an explicit-thread continuation after the change.

This repair cannot reconstruct reasoning that was produced before this bridge
process started, was never observed by the bridge, or no longer has an exact
call/message anchor. Such a task must start a new DeepSeek thread.
