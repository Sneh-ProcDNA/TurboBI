"""Thin async Anthropic client tuned for token efficiency.

Two design choices, both about cost:

1. **Prompt caching at the system message.** The model snapshot
   (table -> [columns]) is sent once and marked with cache_control. Up
   to 50 follow-up resolutions reuse it for ~10% of the original cost.
   Cache TTL is 5 minutes; we don't pay it more than once per workbook
   in practice.

2. **No conversation memory.** Every resolver call is a single
   user-message turn. The model snapshot lives in the system prompt
   (which is what gets cached), so we never resend the workbook context
   in the user turn.

The client is async so the orchestrator can fire all warnings in one
asyncio.gather() and let the API parallelism do the work.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional


# Default to a fast, cheap model. The agent's job here is structured
# extraction, not creative reasoning, so Haiku is the right choice.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class ClaudeClient:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: Optional[str] = None,
        max_tokens: int = 256,
    ):
        # Imported lazily so the module can be imported without the SDK
        # installed (the orchestrator skips the LLM step when no API
        # key is present).
        from anthropic import AsyncAnthropic
        self._client = AsyncAnthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
        )
        self.model = model
        self.max_tokens = max_tokens

    @staticmethod
    def cached_system(text: str) -> List[Dict[str, Any]]:
        """Wrap a long static system prompt in cache_control. The whole
        block becomes a single cache breakpoint — every subsequent
        request that sends the exact same block reads from cache."""
        return [{
            "type": "text",
            "text": text,
            "cache_control": {"type": "ephemeral"},
        }]

    async def ask_json(
        self,
        system: List[Dict[str, Any]] | str,
        user: str,
        max_tokens: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """One-shot turn that expects a JSON-object reply.

        Returns the parsed JSON or None on any failure. We do NOT
        retry — the orchestrator decides whether a missing answer is
        worth a second call. This keeps token spend predictable.
        """
        import json
        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens or self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as e:
            print(f"[CLAUDE] API call failed: {e}")
            return None

        text = "".join(
            getattr(b, "text", "") for b in resp.content
            if getattr(b, "type", "") == "text"
        ).strip()

        # Strip code fences if Claude wrapped the JSON. We don't ask for
        # them, but Haiku occasionally adds them anyway.
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.endswith("```"):
                text = text[: text.rfind("```")]
            text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            print(f"[CLAUDE] non-JSON reply (truncated): {text[:200]!r}")
            return None

    @staticmethod
    def usage_summary(
        responses: List[Optional[Dict[str, Any]]],
    ) -> Dict[str, int]:
        """No-op placeholder. We can't read usage off of ask_json's
        return value without keeping the raw response object — kept here
        so the orchestrator's reporting code has a stable hook to call."""
        return {"calls": sum(1 for r in responses if r is not None)}
