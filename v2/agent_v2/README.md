# Agent V2 — evidence-grounded multi-agent research assistant

English | [中文](./README_zh.md)

Agent V2 is the conversational layer of this repository: a Telegram user asks a
question in Chinese or English ("ARM买入以来跌了这么多，是什么原因？", "why did AMD
drop today", "上面第二点展开讲") and gets an answer in which every figure is tied
to a piece of evidence the system actually fetched — market data, SEC filings,
news pages, the account, the macro board — and verified before it is sent.

It is built without an agent framework: a deterministic planner keyed on a
model-produced intent, an execution engine with fan-out and deadlines, a set of
bounded sub-agents on one generic tool loop, a synthesizer whose drafts must
pass a citation verifier and a model judge, and an evaluation loop that scores
every change on a fixed question set.

## Numbers

Measured through the production runtime and model (DeepSeek) on the VPS; each
question asked twice, majority verdict:

| Run | Set | Cases | Pass | Fallbacks | Avg seconds | Token equivalent / question |
|---|---|---|---|---|---|---|
| dev1 (start of the hardening loop) | dev | 37 | 28 · 76% | 3 | 43.5 | 29,276 |
| dev3 | dev | 37 | 32 · 86% | 0 | 44.1 | 28,286 |
| dev7 | dev | 40 | 37 · 92% | 1 | 35.9 | 25,961 |
| dev8 (final; three investigator cases added) | dev | 43 | 37 · 86% | 1 | 48.5 | 27,008 |
| holdout2 (final; never read while iterating) | hold-out | 13 | 10 · 77% | 0 | 36.4 | 29,816 |
| lab2 (final; one attempt each, minutes per case) | lab | 3 | 3 · 100% | 0 | 88.9 | 30,297 |

* A *fallback* is an answer the verifier rejected twice, replaced by a
  deterministic evidence summary — the answer is safe but flat.
* *Token equivalent* weights uncached input 1, cached input 1/30, output 3, so
  runs at different hours are comparable regardless of provider price tiers.
* 211 offline unit tests and a 39-case offline evaluation (routing, answer
  discipline, two-turn scenarios) run in seconds with a scripted model; the
  CI gate runs them on every push.

On the final development run every one of the six failures passed once and
failed once (the judge disagreeing on a wording criterion, or a question
the model answered differently the second time); none failed twice. The
hold-out set is 15 points below the development set, which is the honest
gap between questions the loop has seen and questions it has not: two of
its three failures were 1/2, one (the colloquial "why is my position down
so much") failed both times and stays open.

## How a question is answered

```
text ─▶ normalize ─▶ intent (model classifier; recorded labels offline)
     ─▶ route ─▶ IntentPlanner (deterministic templates) ─▶ StructuredLLMPlanner (bounded refinement)
     ─▶ ExecutionEngine: DAG of capabilities, fan-out over holdings, wall-clock budget,
        run board for follow-up tasks, cancellation
     ─▶ sub-agents where a capability needs judgement:
        move_attributor · news_checker · filing_reader · investigator · debater
     ─▶ LLMEvidenceSynthesizer: draft → citation completion → verifier + claim judge
        → repair (one or two rounds) → deterministic fallback
     ─▶ debate (one adversarial pass) → bounded revision
     ─▶ result: answer, evidence, verification report, sub-agent traces, cost
```

Two things happen before planning: a classification whose confidence is below
0.6 and which names the gap returns one clarifying question, and the next
message is merged with the original; a question about the previous answer
itself ("第二点展开讲", "为什么这么说") is written from the previous turn's
evidence without new calls.

## Design decisions

**Every figure cites its evidence, and a verifier checks it.** The synthesizer
may only state what the fetched evidence supports and must write the evidence
id after each fact. A deterministic verifier traces every number in the draft
to the cited item (rounding and unit conversions allowed, arithmetic the answer
does not show rejected), checks unknown ids and per-capability rules, and a
model judge decides the wording rules that cannot be pattern-matched ("a
candidate explanation stated as the confirmed cause"). A draft that fails is
repaired with the verifier's exact objections; a second failure falls back to
a deterministic summary rather than shipping an unverified number.
*Cost:* repairs are the largest share of model spend; the hardening loop spent
most of its effort on first-pass rate (restated figures, lead-sentence
citations, side-by-side comparison rows, ranking tables).

**A model judge instead of regular expressions for wording rules.** The first
version enforced "do not call an intraday price a close", "do not expose
internal driver counts" with regexes and produced false positives on every
paraphrase. Rules are now plain-language sentences (`forbid_claim`) judged by
one model call per answer, memoised by content; deterministic checks are kept
only where they are exact (citations, numbers, structure). Soft rules report
without blocking.

**Deterministic planning keyed on a model intent, no regex routing.** The
classifier turns the wording into fields (kind, scope, direction, wants,
tickers, portfolio or watchlist scope, command details, confidence, one
clarifying question). Templates map fields to capabilities: the drawdown chain
with its worst days and sector benchmark, the news plan, the account and
watchlist fan-outs, the market view. Offline, 560 recorded labels replay the
classifier so tests and evaluations need no key. A model planner may refine
research plans but never replaces the fixed templates.

**One generic tool loop for every sub-agent.** Attribution, news checking,
filing reading, the investigator and the debater are declared tools plus a
finish schema on the same `ToolLoop`: native function calling, a JSON-text
fallback, per-loop round and second limits, a forced-finish turn, cancellation
between rounds. A provider quirk (DeepSeek's thinking mode rejects a named
`tool_choice`) is handled once, in the loop.

**Evidence-shaped follow-ups.** The session keeps the previous turn's answer,
evidence and results (slimmed, sixteen sessions at most). A follow-up flagged
by the classifier as being about the previous answer is synthesised from that
evidence: seconds instead of a minute, and verified like any answer.

**Cost accounting the operator can read.** Every model call carries its
run id and source (classifier, planner, each sub-agent, judge, synthesizer);
reports give token equivalents per source, per run and per question, and name
the most expensive questions.

**A quality loop instead of reading Telegram.** Forty development cases with
rubrics (criteria a model grader checks, forbidden assertions, expected route,
sub-agents and source kinds, length bounds, preceding turns for multi-turn
cases, three of them for the investigator), a thirteen-case hold-out set that is run before a release and not
read while iterating, and a three-case lab set (backtest, parameter sweep,
event study; minutes each) graded on the shape of an experiment answer. Repeats with majority verdicts separate model jitter
from regressions. A merge gate runs the unit tests and the offline evaluation
in seconds (`scripts/agent_v2_gate.py`, also on CI) and, with `--live`, the
smoke subset through the real model.

**Data-source health as a ledger, not a log.** Every capability outcome of
every run is recorded (status, seconds, evidence count, first error, "hollow"
results whose limitations say the core modules failed). The report found the
two largest latency sources of the project (research runs timing out on a
mis-planned fan-out; a macro snapshot taking minutes when FRED is slow) and a
threading fault (two workers importing yfinance at once) within a day of
existing.

**Known limitations, stated.** The grader and the graded model are the same
provider, so the pass rate has a bias the hold-out set only partly controls.
Data providers fail silently at times; the system marks the result partial and
says what is missing rather than filling the gap. Pending confirmations,
clarifications and recent turns are kept in sqlite and survive a restart; the
previous turn's full evidence is not, so a follow-up about the previous answer
after a restart is handled as a fresh question, and a chat whose answer the
restart cut off is told to ask again. The investigator sub-agent has three
named jobs (the story of an event, a filing's own terms, the source of a
claim), each with a fixed brief and tool set; three development cases grade
it. The filing-terms and claim-source cases pass reliably; the event-story
case passes most runs and fails when the answer drops a finding's quote, so
it is the least settled part of the system.

## Running it

```
python scripts/agent_v2_gate.py                       # unit tests + offline eval, seconds, no key
python scripts/agent_v2_gate.py --live                # + the quick quality subset through the model
python -m v2.agent_v2.eval.quality run --label NAME --repeat 2 --parallel 2
python -m v2.agent_v2.eval.quality run --set holdout --repeat 2
python -m v2.agent_v2.eval.quality run --set lab --label lab1   # the three experiments, minutes each
python -m v2.agent_v2.eval.quality report --runs 3    # pass rate per run, per-case matrix, criteria missed
python -m v2.agent_v2.eval.quality show q_compare      # one answer with its verdict
python -m v2.agent_v2.eval.subagent_report --since 1  # sub-agent runs, token equivalents per source and per question
python -m v2.agent_v2.eval.capability_report --since 1 # data-source health
python -m v2.agent_v2.eval.intent_report --since 7    # what the classifier decided and how sure it was
```

The live runtime (`runtime.build_workspace_agent`) is what the Telegram bot
uses; the same `AgentV2.run(text, session_id=..., cancel_event=...)` serves the
web facade and the evaluation.

## Layout

```
v2/agent_v2/
  orchestrator.py   run loop: clarification, follow-ups, confirmation, execution, synthesis, debate
  intent.py         classifier, recorded labels, default intent, decision ledger
  planning.py       IntentPlanner templates, budgets, fan-outs
  execution.py      capability registry, engine (DAG, fan-out, deadline, run board, ranking tables)
  llm.py            model planner, synthesizer (draft/repair/shorten/revise), payloads
  verification.py   citation and number tracing, per-capability rules, judged claims
  judge.py          claim judge
  session.py        short-term session: pronouns, pending writes, clarifications, previous turn
  memory.py         user feedback and preferences
  agents/           base.py (ToolLoop, structured_call), move_attributor, news_checker, filing_reader, toolbox (investigator), debater
  adapters/         market, research engine, filings history, web, legacy responders (cached), lab
  interfaces/       telegram, telegram_format, web
  eval/             quality cases and runner, hold-out set, offline suite, benchmark, ledgers and reports
```

The development log with every evaluation round is in
[`eval/reports/2026-09-09-deepseek-vps.md`](./eval/reports/2026-09-09-deepseek-vps.md).
