import { useRef, useState } from "react";
import { postChat } from "../api";
import type { ChatResp } from "../types";

interface Msg {
  role: "user" | "bot";
  html: string;
  chart_b64?: string;
}

const SUGGESTIONS = ["微软资金流怎么样", "我的当日盈亏", "NVDA 为什么动", "特斯拉最近财报"];

export default function ChatPanel() {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);

  async function send(q: string) {
    const query = q.trim();
    if (!query || busy) return;
    setText("");
    setMsgs((m) => [...m, { role: "user", html: query }]);
    setBusy(true);
    try {
      const r: ChatResp = await postChat(query);
      const extras = (r.extra_html || []).map((h) => ({ role: "bot" as const, html: h }));
      setMsgs((m) => [...m, { role: "bot", html: r.html, chart_b64: r.chart_b64 }, ...extras]);
    } catch (e: any) {
      setMsgs((m) => [...m, { role: "bot", html: `❌ ${e.message || e}` }]);
    } finally {
      setBusy(false);
      requestAnimationFrame(() => listRef.current?.scrollTo(0, listRef.current.scrollHeight));
    }
  }

  return (
    <div className="flex flex-col h-full bg-slate-50">
      <div ref={listRef} className="flex-1 overflow-y-auto p-4 space-y-3">
        {msgs.length === 0 && (
          <div className="text-slate-400 text-sm">
            用自然语言问任何东西 —— 组合、资金流、财报、异动、机构持仓…
            <div className="flex flex-wrap gap-2 mt-3">
              {SUGGESTIONS.map((s) => (
                <button key={s} onClick={() => send(s)}
                  className="text-xs px-2 py-1 rounded-full bg-white border border-slate-200 hover:bg-slate-100">
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={m.role === "user" ? "text-right" : ""}>
            <div className={`inline-block max-w-[92%] rounded-2xl px-3 py-2 text-sm ${
              m.role === "user"
                ? "bg-blue-500 text-white"
                : "bg-white border border-slate-200 text-slate-800"
            }`}>
              <div className="card-html" dangerouslySetInnerHTML={{ __html: m.html }} />
              {m.chart_b64 && (
                <img src={`data:image/png;base64,${m.chart_b64}`} alt="chart"
                  className="mt-2 rounded-lg max-w-full" />
              )}
            </div>
          </div>
        ))}
        {busy && <div className="text-slate-400 text-sm">分析中…</div>}
      </div>

      <form className="p-3 border-t border-slate-200 bg-white flex gap-2"
        onSubmit={(e) => { e.preventDefault(); send(text); }}>
        <input value={text} onChange={(e) => setText(e.target.value)}
          placeholder="问点什么…"
          className="flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400" />
        <button type="submit" disabled={busy}
          className="rounded-lg bg-blue-500 text-white px-4 text-sm font-medium disabled:opacity-50">
          发送
        </button>
      </form>
    </div>
  );
}
