"""The quality loop: run the graded questions, score the answers, keep the record, compare runs.

``python -m v2.agent_v2.eval.quality run [--label L] [--cases id,id] [--noweb]``
runs every case through the live agent (the same runtime the bot uses),
grades each answer with :class:`QualityJudge` plus the deterministic
checks, and appends one record per case to ``data/agent_v2_quality.jsonl``.
``... report [--runs N]`` prints the pass rate per run, the per-case
matrix of the last runs and the criteria missed most often.

The judge is one model call per answer (``agent_v2.quality_judge``): each
criterion is met or not, with the sentence that meets it; each forbidden
assertion is made or not, with the sentence that makes it.  Offline (tests)
a scripted judge stands in.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from v2.agent_v2.eval.quality_cases import QUALITY_CASES, QualityCase
from v2.agent_v2.models import AgentResult, sub_agent_summaries

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_PATH = _PROJECT_ROOT / "data" / "agent_v2_quality.jsonl"

#: ``judge(question, answer, criteria, forbidden) -> {"criteria": [{"index", "met", "quote"}], "forbidden": [{"index", "asserted", "quote"}]}``
Judge = Callable[[str, str, list[str], list[str]], dict[str, Any]]

_SYSTEM = """你是投研回答的评分员，不回答用户问题，不要输出任何文字或分析过程，直接调用 grade 工具。
给你用户的问题、助手的回答、一组"必须满足"的标准（criteria，按序号）和一组"不得做出"的断言（forbidden，按序号）。
逐条判断：每条标准回答是否满足（met），满足就引用回答里满足它的那句话（quote，30 字内）；每条断言回答是否做出了（asserted，意思相同即算，带"可能、尚未确认"这类限定的不算），做出了就引用那句话。
只依据回答本身判断，不用你自己的知识补充事实。"""

GRADE_TOOL = {
    "type": "function",
    "function": {
        "name": "grade",
        "description": "逐条给出评分结论。",
        "parameters": {
            "type": "object",
            "properties": {
                "criteria": {"type": "array", "items": {"type": "object", "properties": {"index": {"type": "integer"}, "met": {"type": "boolean"}, "quote": {"type": "string"}}, "required": ["index", "met"]}},
                "forbidden": {"type": "array", "items": {"type": "object", "properties": {"index": {"type": "integer"}, "asserted": {"type": "boolean"}, "quote": {"type": "string"}}, "required": ["index", "asserted"]}},
            },
            "required": ["criteria", "forbidden"],
        },
    },
}


class QualityJudge:
    usage_source_name = "agent_v2.quality_judge"

    def __init__(self, llm: Any, *, max_answer_chars: int = 6000) -> None:
        self.llm = llm
        self.max_answer_chars = max_answer_chars

    def __call__(self, question: str, answer: str, criteria: list[str], forbidden: list[str]) -> dict[str, Any]:
        if self.llm is None:
            raise RuntimeError("quality judge needs a model")
        from v2.agent_v2.agents.base import structured_call
        from v2.usage_context import usage_source

        payload = {"question": question, "answer": (answer or "")[: self.max_answer_chars], "criteria": [{"index": index, "text": text} for index, text in enumerate(criteria)], "forbidden": [{"index": index, "text": text} for index, text in enumerate(forbidden)]}
        with usage_source(self.usage_source_name):
            return structured_call(self.llm, _SYSTEM, payload, GRADE_TOOL)


@dataclass
class QualityScore:
    case_id: str
    passed: bool
    criteria_met: int
    criteria_total: int
    forbidden_hit: int
    route_ok: bool
    agents_ok: bool
    sources_ok: bool
    judged: bool
    #: ``[{"text", "met", "quote"}]`` and ``[{"text", "asserted", "quote"}]``.
    criteria: list[dict[str, Any]] = field(default_factory=list)
    forbidden: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def agents_ran(result: AgentResult) -> set[str]:
    """Every sub-agent the run used, the reader the attributor called nested under it included."""

    ran: set[str] = set()
    for entry in sub_agent_summaries(result.results):
        ran.add(entry["name"])
        if entry.get("nested"):
            ran.add("filing_reader")
    return ran


def grade(case: QualityCase, result: AgentResult, judge: Judge | None) -> QualityScore:
    """Score one answer: the judge on the rubric, the deterministic checks on the run."""

    cited = set()
    import re

    for match in re.finditer(r"\[([A-Za-z0-9_.:-]+)\]", result.answer or ""):
        cited.add(match.group(1))
    cited_sources = {item.source_id for item in result.evidence if item.id in cited}
    sources_ok = not case.must_cite or any(source.startswith(prefix) for source in cited_sources for prefix in case.must_cite)
    route_ok = case.expected_route is None or result.route.kind == case.expected_route
    ran = agents_ran(result)
    agents_ok = all(name in ran for name in case.expected_agents)
    problems: list[str] = []
    if not route_ok:
        problems.append(f"route {result.route.kind.value} != {case.expected_route.value}")
    if not agents_ok:
        problems.append("missing agents: " + ", ".join(sorted(set(case.expected_agents) - ran)))
    if not sources_ok:
        problems.append("cited sources lack " + "/".join(case.must_cite))
    criteria_rows = [{"text": text, "met": None, "quote": ""} for text in case.criteria]
    forbidden_rows = [{"text": text, "asserted": None, "quote": ""} for text in case.forbidden]
    judged = False
    if judge is not None and (case.criteria or case.forbidden):
        try:
            verdict = judge(case.question, result.answer, list(case.criteria), list(case.forbidden))
            for row in verdict.get("criteria") or []:
                index = int(row.get("index", -1))
                if 0 <= index < len(criteria_rows):
                    criteria_rows[index]["met"] = bool(row.get("met"))
                    criteria_rows[index]["quote"] = " ".join(str(row.get("quote") or "").split())[:60]
            for row in verdict.get("forbidden") or []:
                index = int(row.get("index", -1))
                if 0 <= index < len(forbidden_rows):
                    forbidden_rows[index]["asserted"] = bool(row.get("asserted"))
                    forbidden_rows[index]["quote"] = " ".join(str(row.get("quote") or "").split())[:60]
            judged = True
        except Exception as exc:  # noqa: BLE001 — an unjudged answer is recorded as such, not as a pass
            problems.append(f"judge failed: {type(exc).__name__}")
    met = sum(1 for row in criteria_rows if row["met"])
    hit = sum(1 for row in forbidden_rows if row["asserted"])
    for row in criteria_rows:
        if row["met"] is False:
            problems.append("未满足：" + row["text"])
    for row in forbidden_rows:
        if row["asserted"]:
            problems.append("出现禁止断言：" + row["text"] + (f"（“{row['quote']}”）" if row["quote"] else ""))
    passed = route_ok and agents_ok and sources_ok and judged and met == len(criteria_rows) and hit == 0
    if not judged and (case.criteria or case.forbidden):
        problems.append("未评分")
    return QualityScore(case.id, passed, met, len(criteria_rows), hit, route_ok, agents_ok, sources_ok, judged, criteria_rows, forbidden_rows, problems)


def ledger_path() -> Path:
    return Path(os.environ.get("AGENT_V2_QUALITY_LEDGER") or _DEFAULT_PATH)


def record(case: QualityCase, result: AgentResult, score: QualityScore, *, label: str, path: Path | None = None) -> dict[str, Any]:
    row = {
        "at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "label": label,
        "case_id": case.id,
        "origin": case.origin,
        "tags": list(case.tags),
        "question": case.question,
        "answer": result.answer,
        "route": result.route.kind.value,
        "capabilities": [item.capability for item in result.results],
        "sub_agents": sorted(agents_ran(result)),
        "status": result.status.value,
        "synthesis": str((result.synthesis or {}).get("outcome") or ""),
        "elapsed_ms": result.elapsed_ms,
        "run_id": result.run_id,
        "score": asdict(score),
    }
    target = path or ledger_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("quality ledger not written (%s): %s", target, exc)
    return row


def run_cases(agent: Any, cases: tuple[QualityCase, ...], judge: Judge | None, *, label: str, path: Path | None = None, session_prefix: str = "quality") -> list[dict[str, Any]]:
    """Run every case through ``agent`` (a fresh session each), grade and record; returns the rows."""

    rows: list[dict[str, Any]] = []
    for index, case in enumerate(cases, 1):
        started = time.monotonic()
        result = agent.run(case.question, session_id=f"{session_prefix}-{label}-{case.id}", allow_web=case.allow_web)
        score = grade(case, result, judge)
        row = record(case, result, score, label=label, path=path)
        rows.append(row)
        logger.info("quality %s %s %s in %.1fs: %s", label, case.id, "PASS" if score.passed else "FAIL", time.monotonic() - started, "; ".join(score.problems)[:200])
    return rows


def read_rows(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or ledger_path()
    if not target.exists():
        return []
    rows = []
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("case_id"):
                rows.append(row)
    return rows


def summarize(rows: list[dict[str, Any]], *, runs: int = 2) -> dict[str, Any]:
    """Pass rate per run (label), the per-case matrix of the last ``runs`` runs, and the criteria missed most."""

    by_label: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_label.setdefault(str(row.get("label") or "?"), []).append(row)
    labels = list(by_label)
    per_run = []
    for label in labels:
        items = by_label[label]
        latest: dict[str, dict[str, Any]] = {}
        for row in items:
            latest[str(row["case_id"])] = row  # the last record of a case in a run counts
        passed = sum(1 for row in latest.values() if (row.get("score") or {}).get("passed"))
        per_run.append({"label": label, "at": max(str(row.get("at") or "") for row in items), "cases": len(latest), "passed": passed, "rate": round(passed / len(latest), 3) if latest else None, "avg_ms": round(sum(int(row.get("elapsed_ms") or 0) for row in latest.values()) / len(latest)) if latest else 0, "fallbacks": sum(1 for row in latest.values() if row.get("synthesis") == "fallback")})
    recent = labels[-runs:]
    matrix: dict[str, dict[str, Any]] = {}
    for label in recent:
        for row in by_label[label]:
            entry = matrix.setdefault(str(row["case_id"]), {"question": row.get("question"), "runs": {}})
            score = row.get("score") or {}
            entry["runs"][label] = {"passed": bool(score.get("passed")), "problems": list(score.get("problems") or [])[:3], "met": f"{score.get('criteria_met', 0)}/{score.get('criteria_total', 0)}"}
    missed: Counter = Counter()
    for label in recent:
        for row in by_label[label]:
            for item in ((row.get("score") or {}).get("criteria") or []):
                if item.get("met") is False:
                    missed[f"{row['case_id']}: {item['text']}"] += 1
            for item in ((row.get("score") or {}).get("forbidden") or []):
                if item.get("asserted"):
                    missed[f"{row['case_id']}: 禁止 {item['text']}"] += 1
    return {"rows": len(rows), "runs": per_run, "recent": recent, "matrix": matrix, "missed": missed.most_common(15)}


def render(summary: dict[str, Any]) -> str:
    lines = ["# 回答质量报告", ""]
    if not summary["rows"]:
        lines.append("还没有质量评测记录。先跑：python -m v2.agent_v2.eval.quality run --label <名字>")
        return "\n".join(lines)
    lines.append("| 运行 | 时间 | 用例 | 通过 | 通过率 | 平均秒 | 兜底 |")
    lines.append("|---|---|---|---|---|---|---|")
    for run in summary["runs"]:
        rate = "—" if run["rate"] is None else f"{run['rate']:.0%}"
        lines.append(f"| {run['label']} | {run['at'][:16].replace('T', ' ')} | {run['cases']} | {run['passed']} | {rate} | {run['avg_ms'] / 1000:.1f} | {run['fallbacks']} |")
    recent = summary["recent"]
    if recent:
        lines.append("")
        lines.append("| 用例 | " + " | ".join(recent) + " | 最近一次的问题 |")
        lines.append("|---|" + "---|" * len(recent) + "---|")
        for case_id, entry in summary["matrix"].items():
            cells = []
            for label in recent:
                run = entry["runs"].get(label)
                cells.append("—" if run is None else ("✓ " if run["passed"] else "✗ ") + run["met"])
            last = next((entry["runs"][label] for label in reversed(recent) if label in entry["runs"]), None)
            problems = "；".join(last["problems"]) if last and last["problems"] else ""
            lines.append(f"| {case_id} | " + " | ".join(cells) + f" | {problems} |")
    if summary["missed"]:
        lines.append("")
        lines.append("| 最常未满足的标准 | 次数 |")
        lines.append("|---|---|")
        for text, count in summary["missed"]:
            lines.append(f"| {text} | {count} |")
    lines.append("")
    lines.append("通过 = 路由、子智能体、来源类型三项确定性检查都对，评分员判定每条标准满足且没有禁止断言。✓/✗ 后面是满足的标准数。")
    return "\n".join(lines)


def _load_env() -> None:
    """The services get the keys from ``.env`` through systemd; a shell run has to load it itself."""

    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is a runtime dependency on the server
        return
    load_dotenv(_PROJECT_ROOT / ".env")


def _live_agent(*, use_web: bool):
    _load_env()
    if not any(os.environ.get(name) for name in ("AGENT_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")):
        raise SystemExit("没有模型密钥：.env 里需要 DEEPSEEK_API_KEY（或 AGENT_LLM_API_KEY）；服务通过 systemd 读 .env，命令行运行要靠这里加载。")
    from v2.agent_v2.orchestrator import AgentV2Config
    from v2.agent_v2.runtime import build_workspace_agent

    # No sub-agent ledger rows and no intent ledger rows from an eval run; the quality ledger is its own record.
    return build_workspace_agent(config=AgentV2Config(record_sub_agents=False, record_intents=False), enable_web=use_web, use_llm=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run and report the graded quality cases.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the cases through the live agent and grade them")
    run.add_argument("--label", default=datetime.now(tz=timezone.utc).strftime("%m%d-%H%M"), help="name of this run (default: timestamp)")
    run.add_argument("--cases", default="", help="comma-separated case ids (default: all)")
    run.add_argument("--noweb", action="store_true", help="run every case without the web")
    run.add_argument("--feedback", action="store_true", help="also run the cases built from negative user feedback")
    run.add_argument("--path", type=Path, default=None)
    report = sub.add_parser("report", help="pass rate per run and the per-case matrix")
    report.add_argument("--runs", type=int, default=2)
    report.add_argument("--path", type=Path, default=None)
    report.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "report":
        summary = summarize(read_rows(args.path), runs=max(1, args.runs))
        print(json.dumps(summary, ensure_ascii=False, indent=2) if args.json else render(summary))
        return 0
    cases = QUALITY_CASES
    if args.feedback:
        from v2.agent_v2.eval.quality_cases import from_feedback
        from v2.agent_v2.memory import UserMemory

        cases = (*cases, *from_feedback(UserMemory().feedback()))
    wanted = {value.strip() for value in args.cases.split(",") if value.strip()}
    if wanted:
        cases = tuple(case for case in cases if case.id in wanted)
    if args.noweb:
        cases = tuple(QualityCase(**{**asdict(case), "allow_web": False}) for case in cases)
    agent = _live_agent(use_web=not args.noweb)
    judge = QualityJudge(getattr(agent.synthesizer, "llm", None))
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    rows = run_cases(agent, cases, judge, label=args.label, path=args.path)
    passed = sum(1 for row in rows if row["score"]["passed"])
    print(f"{args.label}: {passed}/{len(rows)} passed")
    for row in rows:
        mark = "PASS" if row["score"]["passed"] else "FAIL"
        print(f"  {mark} {row['case_id']} ({row['elapsed_ms'] / 1000:.0f}s): {'; '.join(row['score']['problems'])[:160]}")
    print(render(summarize(read_rows(args.path))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
