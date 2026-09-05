"""Trajectory state and context-window management.

The context window is the loop's real budget. Every tool here returns a fully
formatted card meant for a human on Telegram — ``risk_view`` alone can run past
2 KB — so a naive loop that appends each observation verbatim spends its window
on old, already-summarised data and starts truncating the *question* by step 5.

Two mechanics keep that under control:

* **Recency-weighted compression.** The most recent observations stay intact
  because they drive the next decision. Older ones get clipped head+tail, which
  preserves the card's title and its bottom-line figure while dropping the middle.
* **A running notes field.** When the model states an interim conclusion, that
  text is cheap to carry forward even after the observation behind it is clipped.

Both are deliberately simple and deliberately measurable — ``Trajectory.stats``
reports characters saved, so the compression policy can be tuned against the
eval set instead of by feel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from v2.agent.llm import LLMResponse, ToolCall
from v2.agent.registry import ToolResult


def clip(text: str, limit: int, head_ratio: float = 0.6) -> str:
    """Clip to ``limit`` chars keeping both ends — titles live at the top,
    bottom lines at the bottom, and the middle is usually the repetitive part."""
    if len(text) <= limit:
        return text
    head = int(limit * head_ratio)
    tail = max(limit - head, 0)
    dropped = len(text) - head - tail
    tail_part = text[-tail:] if tail else ""
    return f"{text[:head]}\n…[{dropped} chars elided]…\n{tail_part}"


@dataclass
class Step:
    """One turn of the loop: what the model said, what it called, what came back."""

    index: int
    response: LLMResponse
    results: list[ToolResult] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def tool_calls(self) -> list[ToolCall]:
        return self.response.tool_calls

    @property
    def is_final(self) -> bool:
        return not self.response.tool_calls


@dataclass
class Trajectory:
    """The full record of one agent run — the unit of debugging and of evaluation."""

    query: str
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    started_ms: int = 0
    fresh_observations: int = 3
    stale_chars: int = 600
    _chars_saved: int = 0

    # -- accounting ---------------------------------------------------------
    @property
    def llm_calls(self) -> int:
        return len(self.steps)

    @property
    def tool_calls(self) -> int:
        """Calls that reached a tool — the ones that cost something.

        Refusals (duplicate, over the per-tool cap, malformed arguments) used to
        be counted here, which made the cost metric wrong in the worst
        direction: the cap refusing four calls added four to the count it was
        installed to bring down.
        """
        return sum(1 for s in self.steps for r in s.results if r.reached_tool)

    @property
    def refused_calls(self) -> int:
        """Calls the loop or the gate turned away before anything ran."""
        return sum(1 for s in self.steps for r in s.results if not r.reached_tool)

    @property
    def failed_tool_calls(self) -> int:
        return sum(1 for s in self.steps for r in s.results if not r.ok)

    @property
    def prompt_tokens(self) -> int:
        return sum(s.response.prompt_tokens for s in self.steps)

    @property
    def completion_tokens(self) -> int:
        return sum(s.response.completion_tokens for s in self.steps)

    @property
    def tools_used(self) -> list[str]:
        return [r.name for s in self.steps for r in s.results if r.reached_tool]

    def distinct_tools(self) -> list[str]:
        seen: list[str] = []
        for name in self.tools_used:
            if name not in seen:
                seen.append(name)
        return seen

    def calls_by_tool(self) -> dict[str, int]:
        """How many times each tool was called, most-used first.

        The overspend metric says a run used 25 calls; it does not say whether
        that was one tool fanned out 25 ways or eight tools called three times
        each. Those two have different fixes — a per-tool cap only touches the
        first — and until this existed the difference was unmeasured, so any
        cap would have been a guess.
        """
        counts: dict[str, int] = {}
        for name in self.tools_used:
            counts[name] = counts.get(name, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def tool_records(self) -> list[tuple[str, dict[str, Any], str, bool]]:
        """(tool, args, content, ok) per call — what the attribution check needs."""
        return [(r.name, r.args, r.content, r.ok)
                for s in self.steps for r in s.results]

    def observations_text(self) -> str:
        """Every successful observation concatenated — the grounding corpus."""
        return "\n".join(r.content for s in self.steps for r in s.results if r.ok)

    def call_signature(self, call: ToolCall) -> str:
        return f"{call.name}({sorted((call.arguments or {}).items())})"

    def previous_signatures(self) -> set[str]:
        return {
            f"{r.name}({sorted((r.args or {}).items())})"
            for s in self.steps for r in s.results
        }

    # -- message construction ----------------------------------------------
    def to_messages(self, system_prompt: str) -> list[dict[str, Any]]:
        """Render the trajectory as an OpenAI-format message list.

        Observation age is computed over tool results, not steps, so a step that
        fanned out five parallel calls ages as one batch.
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self.query},
        ]

        total_batches = sum(1 for s in self.steps if s.results)
        batch_index = 0
        for step in self.steps:
            assistant: dict[str, Any] = {"role": "assistant",
                                         "content": step.response.text or None}
            if step.tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name,
                                     "arguments": call.raw_arguments or "{}"},
                    }
                    for call in step.tool_calls
                ]
            messages.append(assistant)

            if not step.results:
                continue
            batch_index += 1
            is_stale = (total_batches - batch_index) >= self.fresh_observations
            for call, result in zip(step.tool_calls, step.results):
                content = result.as_observation()
                if is_stale and len(content) > self.stale_chars:
                    clipped = clip(content, self.stale_chars)
                    self._chars_saved += len(content) - len(clipped)
                    content = clipped
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "name": call.name, "content": content})

        if self.notes:
            messages.append({
                "role": "system",
                "content": "Interim findings you already established:\n- "
                           + "\n- ".join(self.notes[-8:]),
            })
        return messages

    def stats(self) -> dict[str, Any]:
        return {
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "failed_tool_calls": self.failed_tool_calls,
            "distinct_tools": len(self.distinct_tools()),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "context_chars_saved": self._chars_saved,
        }


_SENTENCE_SPLIT = re.compile(r"[。；;\n]|(?<=[.!?])\s")


def extract_note(text: str, max_len: int = 160) -> str:
    """Pull one carry-forward sentence out of an assistant turn.

    Cheap on purpose: the first non-trivial sentence of a reasoning turn is
    almost always its conclusion, and a wrong note costs one clipped line of
    context rather than a wrong answer.
    """
    for part in _SENTENCE_SPLIT.split(text or ""):
        candidate = (part or "").strip()
        if len(candidate) >= 12:
            return candidate[:max_len]
    return ""
