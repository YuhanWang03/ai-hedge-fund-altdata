"""Re-apply the answer key to a saved sweep without re-running the model.

    poetry run python -m v2.agent.eval.rescore data/eval_gpt41mini.json

The JSON a sweep writes carries every answer, the tools each run reached and
the grounding verdict. That is everything ``score_case`` needs except the
model, so a change to the answer key — a date form the labels did not accept,
a fact matcher that treats 57.8 and 57.80 as different numbers — can be
measured against runs that already happened, for free, before anyone decides
it was an improvement. Grounding and attribution are not recomputed: they
need the observations, which the JSON does not keep; their saved verdicts are
reused.

Prints, per mode, the pass count as saved and as re-scored, and every case
whose verdict changed. A re-score that only ever moves cases from fail to
pass is a loosening and should be read as one.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from v2.agent.eval.cases import CASES  # noqa: E402
from v2.agent.eval.holdout import HOLDOUT  # noqa: E402
from v2.agent.eval.scoring import CaseScore, score_case  # noqa: E402

_CASE_BY_ID = {c.id: c for c in CASES + HOLDOUT}


def rescore_row(row: dict[str, Any]) -> CaseScore | None:
    case = _CASE_BY_ID.get(row["case_id"])
    if case is None:
        return None
    tools = list(row.get("calls_by_tool") or {})
    if not tools:
        tools = [t.split("(", 1)[0] for t in row.get("trace") or []]
    return score_case(
        case, mode=row["mode"], answer=row.get("answer", ""), tools_called=tools,
        grounded=row.get("grounded", True), ungrounded=row.get("ungrounded") or (),
        misattributed=row.get("misattributed") or (),
        tool_calls=row.get("tool_calls", 0), tokens=row.get("tokens", 0),
        path=row.get("path", ""), stop_reason=row.get("stop_reason", ""),
        error=row.get("error", ""), trace=row.get("trace") or (),
        repairs=row.get("repairs", 0), draft=row.get("draft", ""),
        draft_findings=row.get("draft_findings") or (),
    )


def rescore(payload: dict[str, Any]) -> dict[str, Any]:
    """{mode: {"saved": n, "rescored": n, "total": n, "changed": [(id, before, after, reason)]}}"""
    out: dict[str, Any] = {}
    for row in payload.get("cases", []):
        if row["mode"] == "baseline":
            continue
        fresh = rescore_row(row)
        if fresh is None:
            continue
        bucket = out.setdefault(row["mode"], {"saved": 0, "rescored": 0, "total": 0, "changed": []})
        bucket["total"] += 1
        bucket["saved"] += 1 if row["passed"] else 0
        bucket["rescored"] += 1 if fresh.passed else 0
        if bool(row["passed"]) != fresh.passed:
            bucket["changed"].append((row["case_id"], row["passed"], fresh.passed,
                                      row.get("reason", "") if row["passed"] is False else "",
                                      fresh.failure_reason()))
    return out


def render(result: dict[str, Any], provider: str) -> str:
    lines = [f"重新打分（模型：{provider}）— 只换答案键，不换答案"]
    for mode, b in result.items():
        delta = b["rescored"] - b["saved"]
        lines.append(f"  {mode:11s} 存档 {b['saved']}/{b['total']}  →  重打 {b['rescored']}/{b['total']}"
                     f"  ({delta:+d})")
        for case_id, before, after, was, now in b["changed"]:
            arrow = "✗→✓" if after else "✓→✗"
            lines.append(f"      {arrow} {case_id}  之前：{was or '通过'}  现在：{now or '通过'}")
    if not result:
        lines.append("  没有模型驱动的运行。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    paths = (argv if argv is not None else sys.argv[1:]) or ["data/eval.json"]
    for raw in paths:
        path = pathlib.Path(raw)
        if not path.is_absolute():
            path = _REPO_ROOT / path
        payload = json.loads(path.read_text(encoding="utf-8"))
        print(f"== {path.name}")
        print(render(rescore(payload), payload.get("provider", "?")))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
