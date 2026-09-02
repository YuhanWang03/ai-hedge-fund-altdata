import { useState } from "react";
import { getToken, setToken } from "./api";
import Dashboard from "./components/Dashboard";
import ChatPanel from "./components/ChatPanel";

export default function App() {
  const [refreshKey, setRefreshKey] = useState(0);
  const [tokenInput, setTokenInput] = useState(getToken());
  const [inject, setInject] = useState<{ text: string; nonce: number } | null>(null);

  function saveToken() {
    setToken(tokenInput.trim());
    setRefreshKey((k) => k + 1);
  }

  const pick = (ticker: string) => setInject({ text: `/flow ${ticker}`, nonce: Date.now() });

  return (
    <div className="h-screen flex flex-col bg-slate-100 text-slate-800">
      <header className="h-12 shrink-0 bg-white border-b border-slate-200 flex items-center px-4 gap-3">
        <span className="font-semibold">📈 AI Hedge Fund</span>
        <span className="text-xs text-slate-400">Dashboard + 聊天</span>
        <div className="ml-auto flex items-center gap-2">
          <input
            type="password"
            value={tokenInput}
            onChange={(e) => setTokenInput(e.target.value)}
            placeholder="owner token"
            className="w-36 rounded border border-slate-300 px-2 py-1 text-xs"
          />
          <button onClick={saveToken} className="text-xs rounded bg-slate-800 text-white px-2 py-1">保存</button>
          <button onClick={() => setRefreshKey((k) => k + 1)} className="text-xs rounded border border-slate-300 px-2 py-1">刷新</button>
        </div>
      </header>

      <div className="flex-1 min-h-0 flex">
        <section className="w-1/2 lg:w-3/5 min-h-0 border-r border-slate-200 bg-slate-50">
          <Dashboard refreshKey={refreshKey} onPick={pick} />
        </section>
        <section className="w-1/2 lg:w-2/5 min-h-0">
          <ChatPanel inject={inject} />
        </section>
      </div>
    </div>
  );
}
