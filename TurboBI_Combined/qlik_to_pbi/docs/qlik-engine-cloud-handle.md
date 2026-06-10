# Qlik Cloud Engine API handle resolution

When talking to Qlik Cloud's Engine API at `wss://<tenant>/app/<app-id>`, **never assume the auto-opened doc lives at handle 1.** Every method call on the wrong handle returns `Invalid Params (code -32602)` — the engine treats a non-existent handle as a bad-params error, not a bad-handle error, so the symptom looks like a parameter problem and misleads diagnosis.

**Why:** Burned by this once — assumed parity with Desktop's path-style URL (which does land at handle 1). Cloud's path-style URL only routes the socket to the right engine pod; the doc isn't necessarily opened, and even when it is, the assigned handle varies across engine versions. Symptom: `GetAppProperties`, `GetAppLayout`, `GetScript`, `CreateSessionObject` ALL fail with `-32602`.

**How to apply:** After every cloud connect, call `GetActiveDoc(-1, [])` first (cheap, no side effects); if that fails or returns no handle, call `OpenDoc(-1, [app_id])` (idempotent against an already-open doc). Only then is the handle safe to use. The same pattern is the right default for any non-Desktop Engine API connection — Desktop's "auto-open at handle 1" is the special case, not the rule.

See `qlik_to_pbi/engine_fetch.py::EngineClient._resolve_cloud_app_handle`.
