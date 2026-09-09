import { useEffect, useRef, useState } from "react";
import { getAgentV2Job, postAgentV2, postChat } from "../api";
import type { AgentV2Evidence, AgentV2Job, AgentV2Resp, ChatResp } from "../types";

type ChatMode = "classic" | "agent_v2";

interface AgentMeta {
  status: string;
  route: string;
  answerMode: string;
  elapsedMs: number;
  verified: boolean;
  capabilities: string[];
  webAllowed: boolean;
  synthesis: string;
}

interface Msg {
  role: "user" | "bot";
  content: string;
  format: "plain" | "html";
  mode?: ChatMode;
  chart_b64?: string;
  agent?: AgentMeta;
  sources?: AgentV2Evidence[];
}

const CLASSIC_SUGGESTIONS = ["微软资金流怎么样", "我的当日盈亏", "NVDA 为什么动", "特斯拉最近财报"];
const AGENT_SUGGESTIONS = ["比较 NVDA 和 AMD 的风险", "分析 AAPL 的估值", "回测 NVDA 动量策略", "什么是自由现金流？"];
const SESSION_KEY = "agentV2SessionId";
const SYNTHESIS_LABELS: Record<string, string> = { clean: "模型回答", repaired: "模型回答（修复一轮）", fallback: "兜底摘要（模型草稿未通过校验）", knowledge: "知识回答", deterministic: "规则摘要" };

function sessionId(): string {
  const stored = sessionStorage.getItem(SESSION_KEY);
  if (stored) return stored;
  const generated = `web-${globalThis.crypto?.randomUUID?.() || Date.now().toString(36)}`;
  sessionStorage.setItem(SESSION_KEY, generated);
  return generated;
}

function uniqueSources(items: AgentV2Evidence[]): AgentV2Evidence[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    if (!item.source_url || seen.has(item.source_url)) return false;
    seen.add(item.source_url);
    return true;
  });
}

function wait(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      window.clearTimeout(timer);
      reject(new DOMException("Request cancelled", "AbortError"));
    }, { once: true });
  });
}

export default function ChatPanel({ inject }: { inject?: { text: string; nonce: number } | null }) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<ChatMode>("classic");
  const [allowWeb, setAllowWeb] = useState(false);
  const [progress, setProgress] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const sessionRef = useRef("");
  const requestRef = useRef<AbortController | null>(null);

  if (!sessionRef.current) sessionRef.current = sessionId();

  useEffect(() => () => requestRef.current?.abort(), []);

  // Dashboard actions retain their existing deterministic chat path.
  useEffect(() => {
    if (inject?.text) send(inject.text, "classic");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inject?.nonce]);

  async function pollJob(initial: AgentV2Job, controller: AbortController): Promise<AgentV2Resp> {
    let job = initial;
    for (let attempt = 0; attempt < 900; attempt += 1) {
      if (job.status === "completed" && job.result) return job.result;
      if (job.status === "failed") throw new Error(job.error || "Agent V2 后台任务失败");
      setProgress(job.progress || `后台任务：${job.agent_status || "running"}`);
      await wait(1000, controller.signal);
      job = await getAgentV2Job(job.job_id, controller.signal);
    }
    throw new Error("Agent V2 后台任务超过 15 分钟仍未完成");
  }

  function appendAgent(result: AgentV2Resp) {
    setMsgs((items) => [
      ...items,
      {
        role: "bot",
        content: result.answer || result.error || "Agent V2 没有返回答案。",
        format: "plain",
        mode: "agent_v2",
        agent: {
          status: result.status,
          route: result.route.kind,
          answerMode: result.answer_mode,
          elapsedMs: result.elapsed_ms,
          verified: result.verification.ok,
          capabilities: result.plan.tasks.map((task) => task.capability),
          webAllowed: result.policy.web_allowed,
          synthesis: result.synthesis?.outcome || "",
        },
        sources: uniqueSources(result.evidence || []),
      },
    ]);
  }

  async function send(q: string, forcedMode?: ChatMode) {
    const query = q.trim();
    if (!query || busy) return;
    const requestMode = forcedMode || mode;
    const controller = new AbortController();
    requestRef.current = controller;
    setText("");
    setMsgs((items) => [...items, { role: "user", content: query, format: "plain", mode: requestMode }]);
    setBusy(true);
    setProgress(requestMode === "agent_v2" ? "Agent V2 正在规划…" : "分析中…");
    try {
      if (requestMode === "agent_v2") {
        const response = await postAgentV2(query, sessionRef.current, allowWeb);
        const result = "job_id" in response ? await pollJob(response, controller) : response;
        appendAgent(result);
      } else {
        const response: ChatResp = await postChat(query);
        const extras = (response.extra_html || []).map((value) => ({
          role: "bot" as const,
          content: value,
          format: "html" as const,
          mode: "classic" as const,
        }));
        setMsgs((items) => [
          ...items,
          { role: "bot", content: response.html, format: "html", mode: "classic", chart_b64: response.chart_b64 },
          ...extras,
        ]);
      }
    } catch (error: any) {
      if (error?.name !== "AbortError") {
        setMsgs((items) => [...items, { role: "bot", content: `❌ ${error?.message || error}`, format: "plain", mode: requestMode }]);
      }
    } finally {
      if (requestRef.current === controller) requestRef.current = null;
      setBusy(false);
      setProgress("");
      requestAnimationFrame(() => listRef.current?.scrollTo(0, listRef.current.scrollHeight));
    }
  }

  const suggestions = mode === "agent_v2" ? AGENT_SUGGESTIONS : CLASSIC_SUGGESTIONS;

  return (
    <div className="flex flex-col h-full bg-slate-50">
      <div className="shrink-0 border-b border-slate-200 bg-white px-3 py-2 flex items-center gap-2">
        <div className="inline-flex rounded-lg border border-slate-200 p-0.5 text-xs">
          <button type="button" onClick={() => setMode("classic")}
            className={`rounded-md px-2.5 py-1 ${mode === "classic" ? "bg-slate-800 text-white" : "text-slate-500"}`}>
            经典
          </button>
          <button type="button" onClick={() => setMode("agent_v2")}
            className={`rounded-md px-2.5 py-1 ${mode === "agent_v2" ? "bg-blue-600 text-white" : "text-slate-500"}`}>
            Agent V2
          </button>
        </div>
        <label className={`ml-auto flex items-center gap-1.5 text-xs ${mode === "agent_v2" ? "text-slate-600" : "text-slate-300"}`}>
          <input type="checkbox" checked={allowWeb} disabled={mode !== "agent_v2"}
            onChange={(event) => setAllowWeb(event.target.checked)}
            className="rounded border-slate-300" />
          允许网页兜底
        </label>
      </div>

      <div ref={listRef} className="flex-1 overflow-y-auto p-4 space-y-3">
        {msgs.length === 0 && (
          <div className="text-slate-400 text-sm">
            {mode === "agent_v2"
              ? "Agent V2 会规划并调用 Research、账户或量化工具，答案附带证据。"
              : "用自然语言问任何东西 —— 组合、资金流、财报、异动、机构持仓…"}
            <div className="flex flex-wrap gap-2 mt-3">
              {suggestions.map((suggestion) => (
                <button key={suggestion} onClick={() => send(suggestion)}
                  className="text-xs px-2 py-1 rounded-full bg-white border border-slate-200 hover:bg-slate-100">
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
        )}
        {msgs.map((message, index) => (
          <div key={index} className={message.role === "user" ? "text-right" : ""}>
            <div className={`inline-block max-w-[92%] rounded-2xl px-3 py-2 text-sm text-left ${
              message.role === "user"
                ? "bg-blue-500 text-white"
                : "bg-white border border-slate-200 text-slate-800"
            }`}>
              {message.role === "user" && (
                <div className="mb-1 text-[10px] opacity-70">{message.mode === "agent_v2" ? "Agent V2" : "经典"}</div>
              )}
              {message.agent && (
                <div className="mb-2 flex flex-wrap gap-1 text-[10px]">
                  <span className="rounded bg-blue-50 px-1.5 py-0.5 text-blue-700">{message.agent.route}</span>
                  <span className="rounded bg-slate-100 px-1.5 py-0.5">{message.agent.answerMode}</span>
                  <span className={`rounded px-1.5 py-0.5 ${message.agent.verified ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700"}`}>
                    {message.agent.verified ? "证据校验通过" : "证据校验有警告"}
                  </span>
                  {message.agent.synthesis && (
                    <span className={`rounded px-1.5 py-0.5 ${message.agent.synthesis === "fallback" ? "bg-amber-50 text-amber-700" : "bg-slate-100"}`}>
                      {SYNTHESIS_LABELS[message.agent.synthesis] || message.agent.synthesis}
                    </span>
                  )}
                  {message.agent.webAllowed && <span className="rounded bg-violet-50 px-1.5 py-0.5 text-violet-700">Web</span>}
                  <span className="rounded bg-slate-100 px-1.5 py-0.5">{(message.agent.elapsedMs / 1000).toFixed(1)}s</span>
                </div>
              )}
              {message.format === "html"
                ? <div className="card-html" dangerouslySetInnerHTML={{ __html: message.content }} />
                : <div className="whitespace-pre-wrap break-words">{message.content}</div>}
              {message.chart_b64 && (
                <img src={`data:image/png;base64,${message.chart_b64}`} alt="chart"
                  className="mt-2 rounded-lg max-w-full" />
              )}
              {message.agent && message.agent.capabilities.length > 0 && (
                <details className="mt-2 border-t border-slate-100 pt-2 text-xs text-slate-500">
                  <summary className="cursor-pointer">执行工具（{message.agent.capabilities.length}）</summary>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {message.agent.capabilities.map((capability, capabilityIndex) => (
                      <code key={`${capability}-${capabilityIndex}`} className="rounded bg-slate-100 px-1 py-0.5">{capability}</code>
                    ))}
                  </div>
                </details>
              )}
              {message.sources && message.sources.length > 0 && (
                <details className="mt-2 border-t border-slate-100 pt-2 text-xs">
                  <summary className="cursor-pointer text-slate-500">证据来源（{message.sources.length}）</summary>
                  <ul className="mt-1 space-y-1">
                    {message.sources.map((source) => (
                      <li key={source.source_url}>
                        <a href={source.source_url} target="_blank" rel="noreferrer"
                          className="text-blue-600 hover:underline">
                          {source.source_title || source.source_id || source.source_url}
                        </a>
                        {source.as_of && <span className="ml-1 text-slate-400">{source.as_of.slice(0, 10)}</span>}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </div>
          </div>
        ))}
        {busy && <div className="text-slate-400 text-sm">{progress || "分析中…"}</div>}
      </div>

      <form className="p-3 border-t border-slate-200 bg-white flex gap-2"
        onSubmit={(event) => { event.preventDefault(); send(text); }}>
        <input value={text} onChange={(event) => setText(event.target.value)}
          placeholder={mode === "agent_v2" ? "让 Agent V2 研究、比较或运行实验…" : "问点什么…"}
          className="flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400" />
        <button type="submit" disabled={busy}
          className="rounded-lg bg-blue-500 text-white px-4 text-sm font-medium disabled:opacity-50">
          发送
        </button>
      </form>
    </div>
  );
}
