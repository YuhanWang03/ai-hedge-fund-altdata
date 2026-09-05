"""Suite runner — executes the cases under several configurations and compares.

One number for "the agent" is not worth much. What a reader needs to know is
whether each mechanism paid for itself, so the suite runs the same 83 cases under
ablations that each remove exactly one thing:

    baseline           the incumbent single-hop router — the thing to beat
    agent              the full loop
    routed             the router picks per query; this is what production does
    agent_no_parallel  fan-out disabled — isolates the latency win
    agent_no_repair    grounding repair disabled — isolates what the repair buys
    agent_tight        3 steps / 4 tool calls — how much does budget matter

``routed`` is the interesting row: it should land near ``agent`` on pass rate
while costing much closer to ``baseline``, and if it does not, the router's
signal table is wrong.

Cases run concurrently because each is independent and a real-model sweep is
otherwise dominated by wall-clock. The tool layer is recorded, so the only
non-determinism is the model itself.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from v2.agent import grounding, router
from v2.agent.baseline import run_baseline
from v2.agent.eval.cases import CASES, EvalCase
from v2.agent.eval.fixtures import build_eval_registry
from v2.agent.eval.scoring import CaseScore, SuiteReport, score_case
from v2.agent.loop import AgentConfig, run_agent


@dataclass(frozen=True)
class Mode:
    name: str
    kind: str                      # "baseline" | "agent" | "routed"
    config: AgentConfig | None = None
    description: str = ""


MODES: dict[str, Mode] = {
    "baseline": Mode("baseline", "baseline", None, "单跳路由（现状）"),
    "agent": Mode("agent", "agent", AgentConfig(), "完整 agent 循环"),
    "routed": Mode("routed", "routed", AgentConfig(), "路由决定走哪条（生产形态）"),
    "agent_no_parallel": Mode("agent_no_parallel", "agent",
                              AgentConfig(parallel=False), "关闭并行扇出"),
    "agent_no_repair": Mode("agent_no_repair", "agent",
                            AgentConfig(grounding_repair=False), "关闭溯源重写"),
    "agent_tight": Mode("agent_tight", "agent",
                        AgentConfig(max_steps=3, max_tool_calls=4), "预算收紧"),
    # What the bot actually runs with. Every sweep so far measured the loop at
    # its harness default of 20 calls; production caps it at 8. So the
    # overspend being chased (17–21% at 20 calls) is behaviour production never
    # exhibits, and the question that matters — what does the 8-call cap cost
    # in pass rate — had never been asked. The numbers must match
    # ``bot_bridge.production_config()``'s defaults; a test holds them together.
    "production": Mode("production", "routed",
                       AgentConfig(max_steps=5, max_tool_calls=8,
                                   max_calls_per_tool=8, max_seconds=90.0),
                       "生产配置（路由 + 5 步 / 8 次调用）"),
}

DEFAULT_MODES = ("baseline", "routed", "production", "agent")


def _parsed(case: EvalCase) -> dict[str, Any]:
    """The classifier output the case is labelled with.

    Using the label rather than a live classification keeps the suite free of one
    extra LLM call per case and makes the baseline deterministic, so a change in
    agent scores can never be an artefact of the classifier drifting.
    """
    parsed = {"intent": case.intent, "ticker": case.ticker, "manager": "",
              "etf": "", "target_price": 0.0, "direction": "", "days_horizon": 0,
              "period": "", "days_back": 0, "release_type": "", "raw": case.query}
    parsed.update(case.extra)
    return parsed


def run_case(case: EvalCase, mode: Mode, *, llm_factory: Callable[[], Any]) -> CaseScore:
    """Run one case under one mode. Never raises — a crash is a scored failure."""
    registry = build_eval_registry()
    parsed = _parsed(case)
    started = time.time()

    kind = mode.kind
    path = ""
    if kind == "routed":
        decision = router.route(case.query, parsed, mode="heuristic")
        path = decision.path
        kind = "agent" if decision.is_agent else "baseline"
    elif kind == "baseline":
        path = "single_hop"
    else:
        path = "agent"

    try:
        if kind == "baseline":
            result = run_baseline(case.query, classifier=lambda _t: parsed,
                                  registry=registry)
            return score_case(
                case, mode=mode.name, answer=result.answer,
                tools_called=[result.tool] if result.tool else [],
                grounded=True, tool_calls=1 if result.tool else 0, llm_calls=1,
                tokens=0, elapsed_ms=result.elapsed_ms, path=path,
                stop_reason="single_hop",
                trace=[result.tool] if result.tool else [])

        result = run_agent(case.query, llm=llm_factory(), registry=registry,
                           config=mode.config or AgentConfig())
        trajectory = result.trajectory
        return score_case(
            case, mode=mode.name, answer=result.answer,
            tools_called=trajectory.distinct_tools(),
            grounded=result.grounding.ok,
            ungrounded=result.grounding.ungrounded,
            derived=result.grounding.derived,
            ungrounded_kinds=(grounding.diagnose(result.grounding,
                                                 trajectory.observations_text())
                              if not result.grounding.ok else None),
            # The finding *and* the line it came from: a verdict with no
            # evidence is a verdict nobody can act on.
            misattributed=tuple(
                f"{entity}←{figure}(实为 {'/'.join(owners)})｜{evidence}"
                for (entity, figure, owners), evidence in zip(
                    result.attribution.misattributed,
                    list(result.attribution.evidence) + [""] * 8)),
            calls_by_tool=trajectory.calls_by_tool(),
            capped_calls=result.capped_calls,
            refused_calls=trajectory.refused_calls,
            tool_calls=trajectory.tool_calls, llm_calls=trajectory.llm_calls,
            tokens=trajectory.prompt_tokens + trajectory.completion_tokens,
            elapsed_ms=result.elapsed_ms, path=path,
            stop_reason=result.stop_reason, error=result.error,
            trace=trajectory.trace(), repairs=result.repairs,
            draft=result.draft, draft_findings=result.draft_findings)
    except Exception as exc:  # noqa: BLE001 — one bad case must not kill the sweep
        return score_case(case, mode=mode.name, answer="", tools_called=[],
                          grounded=False, elapsed_ms=int((time.time() - started) * 1000),
                          path=path, error=f"{type(exc).__name__}: {exc}")


def run_suite(
    mode_name: str,
    *,
    llm_factory: Callable[[], Any],
    cases: tuple[EvalCase, ...] = CASES,
    workers: int = 4,
    repeat: int = 1,
    on_case: Callable[[CaseScore], None] | None = None,
) -> SuiteReport:
    """Run every case under one mode, optionally several times.

    Repeats exist because two sweeps of the identical configuration scored the
    same 66/83 while sharing only 9 of 25 distinct failures — roughly two thirds
    of a single run's failure list is noise. Acting on one run therefore means
    spending effort on cases that were never broken. Separating a stable failure
    from a flaky one needs more than one sample, and nothing else can substitute.

    The baseline is deterministic, so repeating it would only cost wall-clock.
    """
    mode = MODES[mode_name]
    if mode.kind == "baseline":
        repeat = 1
    report = SuiteReport(mode=mode_name, repeat=repeat)
    work = [c for c in cases for _ in range(repeat)]

    def _work(case: EvalCase) -> CaseScore:
        score = run_case(case, mode, llm_factory=llm_factory)
        if on_case is not None:
            on_case(score)
        return score

    if workers <= 1:
        report.scores = [_work(c) for c in work]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            report.scores = list(pool.map(_work, work))
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_RULE = "─" * 100


def _fmt_int(value: float) -> str:
    return "—" if value in (0, None) else f"{value:,.0f}"


def render_comparison(reports: list[SuiteReport]) -> str:
    lines = [_RULE, "【模式对比】", _RULE,
             f"  {'mode':<20}{'通过':>9}{'通过率':>8}{'工具召回':>9}{'事实召回':>9}"
             f"{'溯源率':>8}{'浪费':>7}{'超预算':>8}{'工具/例':>9}{'token/例':>10}{'每通过token':>12}"]
    for report in reports:
        row = report.summary()
        per_case = row["total_tokens"] / row["total"] if row["total"] else 0.0
        per_pass = row["tokens_per_pass"]
        lines.append(
            f"  {row['mode']:<20}"
            f"{row['passed']}/{row['total']:<4}".rjust(9)
            + f"{row['pass_rate']:>8.0%}"
            + f"{row['tool_recall']:>9.0%}"
            + f"{row['fact_recall']:>9.0%}"
            + f"{row['grounded_rate']:>8.0%}"
            + f"{row['waste_rate']:>7.0%}"
            + f"{row['overspend_rate']:>8.0%}"
            + f"{row['mean_tool_calls']:>9.1f}"
            + f"{_fmt_int(per_case):>10}"
            + f"{('∞' if per_pass == float('inf') else format(per_pass, ',.0f')):>12}"
        )
    return "\n".join(lines)


def render_categories(reports: list[SuiteReport]) -> str:
    categories = sorted({c for r in reports for c in r.by_category()})
    header = f"  {'category':<18}" + "".join(f"{r.mode:>20}" for r in reports)
    lines = [_RULE, "【分类通过率】", _RULE, header]
    for category in categories:
        cells = ""
        for report in reports:
            passed, total = report.by_category().get(category, (0, 0))
            cells += f"{f'{passed}/{total} ({passed / total:.0%})' if total else '—':>20}"
        lines.append(f"  {category:<18}{cells}")
    return "\n".join(lines)


def render_stability(report: SuiteReport) -> str:
    """Separate real failures from run-to-run noise."""
    if report.repeat <= 1:
        return (_RULE + f"\n【{report.mode} 稳定性】\n" + _RULE +
                "\n  只跑了一轮 —— 无法区分真失败和抖动。"
                "\n  同配置两轮实测：分数相同，但 25 条不同的失败里只有 9 条重合（36%）。"
                "\n  要据此改代码，请用 --repeat 3。")
    flaky = report.flaky()
    stable = report.stable_failures()
    lines = [_RULE, f"【{report.mode} 稳定性】每条跑了 {report.repeat} 次", _RULE,
             f"  稳定失败（每次都挂，值得改）：{len(stable)} 条  {', '.join(stable) or '无'}",
             f"  抖动（有时过有时挂，先别急着改）：{len(flaky)} 条"]
    for case_id, passed, total in flaky:
        lines.append(f"    · {case_id}  {passed}/{total} 次通过")
    return "\n".join(lines)


_KIND_LABEL = {
    "error": "运行错误（模型侧/网络，循环里没有可修的东西）",
    "budget": "预算（路径和通过的一样，只是没走完就被截停）",
    "early_stop": "提前收尾（路径是通过路径的前缀，模型自己决定够了就写了）",
    "tool_choice": "工具选择（同样的上下文，模型选了不同的工具或参数）",
    "wording": "措辞（路径、证据完全相同，只是写出来的答案不同）",
}


def _short(call: str, width: int = 34) -> str:
    return call if len(call) <= width else call[: width - 1] + "…"


def render_divergence(report: SuiteReport) -> str:
    """For each flaky case: where its failing runs left the passing path.

    Temperature is already zero, so the flaky list cannot be shrunk by a
    sampling knob; it can only be split by cause. A tool-choice fork points at
    a prompt or a description, a wording fork at the answer check, a budget
    fork at the stop rule, and an error at the provider — four different
    places to look, and the pass ratio alone never said which.
    """
    if report.repeat <= 1:
        return ""
    flaky = report.flaky()
    lines = [_RULE, f"【{report.mode} 抖动分叉点】{len(flaky)} 条", _RULE]
    if not flaky:
        lines.append("  没有抖动的 case。")
        return "\n".join(lines)
    by_kind = report.flaky_by_kind()
    lines.append("  按分叉类型：" + " · ".join(
        f"{kind} {len(ids)}" for kind, ids in by_kind.items()))
    lines.append("")
    for case_id, passed, total in flaky:
        d = report.divergence(case_id)
        lines.append(f"  · {case_id}  {passed}/{total} 通过 → {_KIND_LABEL.get(d.kind, d.kind)}")
        good = " → ".join(_short(c) for c in d.good_trace) or "(未调用工具)"
        lines.append(f"      通过路径：{good}")
        if d.kind == "wording":
            lines.append(f"      失败路径：同上；失败原因：{'；'.join(d.reasons)}")
        else:
            marked = []
            for i, call in enumerate(d.bad_trace):
                text = _short(call)
                marked.append(f"⟨{text}⟩" if d.fork_at is not None and i == d.fork_at else text)
            if d.fork_at is not None and d.fork_at >= len(d.bad_trace):
                marked.append("⟨停⟩")
            bad = " → ".join(marked) or "(未调用工具)"
            where = f"第 {d.fork_at + 1} 步分叉" if d.fork_at is not None else "无通过样本可比"
            lines.append(f"      失败路径：{bad}   ({where}"
                         + (f" · {'/'.join(d.bad_stops)}" if d.bad_stops else "") + ")")
            lines.append(f"      失败原因：{'；'.join(d.reasons)}")
    lines.append("")
    lines.append("  ⟨⟩ 标出失败路径里第一处与通过路径不同的调用；"
                 "完整路径和答案原文在 JSON 的 trace / answer 字段里。")
    return "\n".join(lines)


def render_failures(report: SuiteReport, limit: int = 20) -> str:
    seen: set[str] = set()
    failures = []
    for score in report.failures():
        if score.case_id not in seen:
            seen.add(score.case_id)
            failures.append(score)
    stability = report.stability()
    lines = [_RULE,
             f"【{report.mode} 的失败清单】{len(failures)} 条"
             + (f"（每条跑 {report.repeat} 次，括号内为通过次数）" if report.repeat > 1 else ""),
             _RULE]
    if not failures:
        lines.append("  无失败。")
        return "\n".join(lines)
    case_by_id = {c.id: c for c in CASES}
    for score in failures[:limit]:
        case = case_by_id.get(score.case_id)
        passed, total = stability.get(score.case_id, (0, 1))
        tag = f" [{passed}/{total} 通过]" if report.repeat > 1 else ""
        lines.append(f"  ✗ [{score.case_id}]{tag} {case.query if case else ''}")
        budget = f" · ⚠️ 超预算（上限 {case.max_tool_calls}）" if score.overspend and case else ""
        lines.append(f"      {score.failure_reason()}"
                     f"   (工具 {score.tool_calls} 次 · {score.tokens} token"
                     f" · {score.stop_reason}{budget})")
        if case and case.note:
            lines.append(f"      标注说明：{case.note}")
    if len(failures) > limit:
        lines.append(f"  …另有 {len(failures) - limit} 条，完整清单见 JSON 输出")
    return "\n".join(lines)


def render_attribution(report: SuiteReport) -> str:
    """Every misattribution warning raised on an answer the case says is correct.

    Read this as the *checker's* error rate, not the model's: the case's own
    assertions already decided the answer was right, so anything listed here is
    the check rejecting good work. The axis exists because for nine rounds it
    did not: seven false positives shipped, each found by a human reading a
    Telegram message rather than by this suite.
    """
    findings = report.false_misattributions()
    lines = [_RULE,
             f"【{report.mode} 归属检查的误报】{len(findings)} / {report.total} 条用例",
             _RULE]
    if not findings:
        lines.append("  无 —— 正确的回答没有被打上张冠李戴的标记。")
        return "\n".join(lines)
    for case_id, pairs in findings[:12]:
        for pair in pairs[:2]:
            finding, _, evidence = pair.partition("｜")
            lines.append(f"  {case_id:<6}{finding}")
            if evidence:
                lines.append(f"        ↳ {evidence[:110]}")
    if len(findings) > 12:
        lines.append(f"  …另有 {len(findings) - 12} 条")
    lines.append("")
    lines.append("  这些回答通过了自己全部的事实与禁词断言,警告是检查器的错,不是模型的。")
    return "\n".join(lines)


def render_repairs(report: SuiteReport) -> str:
    """Repair rounds, and the ones that made the answer worse.

    A rewrite the checks demanded is supposed to fix a figure. When the draft
    already had every fact the case asks for and the rewrite does not, the
    check has cost a correct answer — and until this existed that cost was
    booked as 「事实缺失」 against the model, with the check's own error rate
    sitting at zero. Printed whenever any repair ran.
    """
    repaired = [s for s in report.scores if s.repairs]
    if not repaired:
        return ""
    regressed = report.repair_regressions()
    lines = [_RULE,
             f"【{report.mode} 重写】{len(repaired)} 次运行被打回重写，"
             f"其中 {len(regressed)} 次重写丢了初稿已有的事实",
             _RULE]
    if not regressed:
        lines.append("  没有重写把正确的初稿改坏。")
        return "\n".join(lines)
    for score in regressed[:8]:
        lines.append(f"  · [{score.case_id}] 打回原因：{'；'.join(score.draft_findings[:4])}")
        lines.append(f"      重写后：{score._own_reason()}")
    lines.append("")
    lines.append("  这一栏是校验的另一种错误率：警告打在了正确的初稿上，重写把事实一起扔了。"
                 "初稿原文在 JSON 的 draft 字段里。")
    return "\n".join(lines)


def render_grounding(report: SuiteReport) -> str:
    """Split the rejected figures by why they failed.

    This is the difference between "tighten the prompt" and "relax the check":
    figures that some sum in the observations reproduces are the model failing to
    show its arithmetic, while ``unknown`` figures have no arithmetic explanation
    at all and are the ones actually worth calling fabrication.
    """
    counts = report.ungrounded_breakdown()
    total = sum(counts.values())
    lines = [_RULE, f"【{report.mode} 溯源失败的数字来自哪里】共 {total} 个", _RULE]
    if not total:
        lines.append("  无。")
        return "\n".join(lines)
    labels = {"rounding": "四舍五入/位数不同（观测里有近似值）",
              "sum": "是观测中若干数字之和（模型没写清算式）",
              "difference": "是观测中两数之差（同上）",
              "unknown": "观测里找不到任何算术来源 —— 这些才是真正的编造"}
    for kind in grounding.FIGURE_KINDS:
        count = counts.get(kind, 0)
        if count:
            lines.append(f"  {labels[kind]:<38}{count:>4}  ({count / total:.0%})")
    derived = report.summary()["derived_figures"]
    lines.append(f"\n  另有 {derived} 个数字是靠答案里**写出的算式**被接受的"
                 "（输入本身可溯源 + 算式显式给出）。")
    lines.append("  注：sum/difference 只说明「存在一个算术解释」，不等于模型真做了该运算；"
                 "\n      观测里数字一多就可能巧合命中。只有 unknown 可以放心当作编造处理。")
    return "\n".join(lines)


def render_overspend(report: SuiteReport, limit: int = 10) -> str:
    """Cases that blew their tool budget, whether or not they answered correctly.

    Over-calling does not fail a case — it is a cost problem, not a correctness
    one — so it would otherwise vanish into a mean. It is also the clearest
    signal of where the loop explores instead of deciding.
    """
    over = sorted([s for s in report.scores if s.overspend],
                  key=lambda s: s.tool_calls, reverse=True)
    lines = [_RULE, f"【{report.mode} 超预算的 case】共 {len(over)} 条", _RULE]
    if not over:
        lines.append("  无。")
        return "\n".join(lines)
    case_by_id = {c.id: c for c in CASES}
    for score in over[:limit]:
        case = case_by_id.get(score.case_id)
        lines.append(f"  · [{score.case_id}] {case.query if case else ''}"
                     f"  {score.tool_calls} 次 / 上限 {case.max_tool_calls if case else '?'}"
                     f" · {score.tokens} token"
                     f" · {'通过' if score.passed else '未通过'}")
        # Depth or breadth? The count alone cannot say, and the two have
        # different fixes — a per-tool cap only ever touches depth.
        top = list(score.calls_by_tool.items())[:4]
        if top:
            shape = " · ".join(f"{name}×{count}" for name, count in top)
            rest = len(score.calls_by_tool) - len(top)
            lines.append(f"        ↳ {shape}" + (f" …另 {rest} 个工具" if rest > 0 else "")
                         + (f" · 另有 {score.refused_calls} 次被拒（其中上限 "
                            f"{score.capped_calls} 次，不计入上面的次数）"
                            if score.refused_calls else ""))
    lines.append("")
    lines.append("  ↳ 那一行是每个工具各调了几次：一个工具扇出很多次是「深度」，"
                 "很多工具各调几次是「广度」——只有前者能被 per-tool 上限拦住。")
    return "\n".join(lines)


def render_routing(report: SuiteReport) -> str:
    """Routing accuracy measured on the same set as answer quality."""
    wrong = [s for s in report.scores if not s.path_correct]
    lines = [_RULE, "【路由】", _RULE,
             f"  准确率 {report.summary()['routing_accuracy']:.0%}"
             f"（{report.total - len(wrong)}/{report.total}）"]
    case_by_id = {c.id: c for c in CASES}
    for score in wrong[:12]:
        case = case_by_id.get(score.case_id)
        lines.append(f"  ✗ [{score.case_id}] {case.query if case else ''}"
                     f"  期望 {case.expected_path if case else '?'} 实际 {score.path}")
    return "\n".join(lines)


def to_json(reports: list[SuiteReport]) -> dict:
    return {
        "summaries": [r.summary() for r in reports],
        "cases": [
            {
                "mode": s.mode, "case_id": s.case_id, "category": s.category,
                "passed": s.passed, "reason": s.failure_reason(),
                "tool_recall": s.tool_recall, "fact_recall": s.fact_recall,
                "grounded": s.grounded, "missing_tools": list(s.missing_tools),
                "missing_facts": list(s.missing_facts),
                "violations": list(s.violations), "forbidden": list(s.forbidden_hit),
                "ungrounded": list(s.ungrounded), "overspend": s.overspend,
                "misattributed": list(s.misattributed),
                "calls_by_tool": s.calls_by_tool, "capped_calls": s.capped_calls,
                "refused_calls": s.refused_calls,
                "ungrounded_kinds": s.ungrounded_kinds,
                "tool_calls": s.tool_calls, "llm_calls": s.llm_calls,
                "tokens": s.tokens, "elapsed_ms": s.elapsed_ms,
                "path": s.path, "path_correct": s.path_correct,
                "stop_reason": s.stop_reason, "error": s.error,
                "trace": list(s.trace), "answer": s.answer,
                "repairs": s.repairs, "draft": s.draft,
                "draft_findings": list(s.draft_findings),
                "draft_facts_ok": s.draft_facts_ok,
                "repair_regressed": s.repair_regressed,
            }
            for r in reports for s in r.scores
        ],
    }
