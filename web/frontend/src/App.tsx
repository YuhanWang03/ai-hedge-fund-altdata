import { useEffect, useState } from "react";
import { getToken, setToken } from "./api";
import Dashboard from "./components/Dashboard";
import ChatPanel from "./components/ChatPanel";

const LIVE_MS = 60_000;   // ticker / portfolio / risk / equity / flow
const SLOW_MS = 300_000;  // recommendations (30-ticker scan — keep it light)

export default function App() {
  const [manualKey, setManualKey] = useState(0);
  const [liveTick, setLiveTick] = useState(0);
  const [slowTick, setSlowTick] = useState(0);
  const [autoOn, setAutoOn] = useState(true);
  const [tokenInput, setTokenInput] = useState(getToken());
  const [inject, setInject] = useState<{ text: string; nonce: number } | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date>(new Date());

  // Auto-refresh timers — two cadences so the expensive scan stays light.
  useEffect(() => {
    if (!autoOn) return;
    const a = setInterval(() => setLiveTick((t) => t + 1), LIVE_MS);
    const b = setInterval(() => setSlowTick((t) => t + 1), SLOW_MS);
    return () => { clearInterval(a); clearInterval(b); };
  }, [autoOn]);

  const liveKey = manualKey + liveTick;
  const slowKey = manualKey + slowTick;

  useEffect(() => { setLastUpdated(new Date()); }, [liveKey]);

  function saveToken() {
    setToken(tokenInput.trim());
    setManualKey((k) => k + 1);
  }
  const pick = (ticker: string) => setInject({ text: `/flow ${ticker}`, nonce: Date.now() });

  return (
    <div className="h-screen flex flex-col bg-slate-100 text-slate-800">
      <header className="h-12 shrink-0 bg-white border-b border-slate-200 flex items-center px-4 gap-3">
        <span className="font-semibold">📈 AI Hedge Fund</span>
        <span className="text-xs text-slate-400">Dashboard + 聊天</span>
        <div className="ml-auto flex items-center gap-2">
          <span className="text-[11px] text-slate-400">
            更新 {lastUpdated.toLocaleTimeString("zh-CN", { hour12: false })}
          </span>
          <button
            onClick={() => setAutoOn((v) => !v)}
            title={`自动刷新：实时 ${LIVE_MS / 1000}s / 推荐 ${SLOW_MS / 1000}s`}
            className={`text-xs rounded px-2 py-1 ${
              autoOn ? "bg-emerald-500 text-white" : "border border-slate-300 text-slate-500"
            }`}
          >
            自动{autoOn ? "开" : "关"}
          </button>
          <input
            type="password"
            value={tokenInput}
            onChange={(e) => setTokenInput(e.target.value)}
            placeholder="owner token"
            className="w-32 rounded border border-slate-300 px-2 py-1 text-xs"
          />
          <button onClick={saveToken} className="text-xs rounded bg-slate-800 text-white px-2 py-1">保存</button>
          <button onClick={() => setManualKey((k) => k + 1)} className="text-xs rounded border border-slate-300 px-2 py-1">刷新</button>
        </div>
      </header>

      <div className="flex-1 min-h-0 flex">
        <section className="w-1/2 lg:w-3/5 min-h-0 border-r border-slate-200 bg-slate-50">
          <Dashboard refreshKey={liveKey} recoKey={slowKey} onPick={pick} />
        </section>
        <section className="w-1/2 lg:w-2/5 min-h-0">
          <ChatPanel inject={inject} />
        </section>
      </div>
    </div>
  );
}
