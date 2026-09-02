import { useEffect, useState } from "react";
import { getPortfolio } from "../api";
import type { PortfolioResp } from "../types";

const money = (n: number) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const pct = (n: number) => `${n >= 0 ? "+" : ""}${(n * 100).toFixed(2)}%`;
const signColor = (n: number) => (n > 0 ? "text-emerald-600" : n < 0 ? "text-rose-600" : "text-slate-500");

function Sparkline({ values }: { values: number[] }) {
  if (values.length < 2) return null;
  const w = 240, h = 44, min = Math.min(...values), max = Math.max(...values);
  const span = max - min || 1;
  const pts = values
    .map((v, i) => `${(i / (values.length - 1)) * w},${h - ((v - min) / span) * h}`)
    .join(" ");
  const up = values[values.length - 1] >= values[0];
  return (
    <svg width={w} height={h} className="mt-1">
      <polyline points={pts} fill="none" strokeWidth={2}
        stroke={up ? "#059669" : "#e11d48"} />
    </svg>
  );
}

export default function PortfolioPanel({ refreshKey }: { refreshKey: number }) {
  const [data, setData] = useState<PortfolioResp | null>(null);
  const [error, setError] = useState<string>("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError("");
    getPortfolio()
      .then((d) => alive && setData(d))
      .catch((e) => alive && setError(String(e.message || e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [refreshKey]);

  if (loading && !data) return <div className="p-4 text-slate-400">加载组合中…</div>;
  if (error) return (
    <div className="p-4">
      <div className="rounded-lg bg-rose-50 border border-rose-200 p-3 text-sm text-rose-700">
        无法获取组合：<code>{error}</code>
        <div className="mt-1 text-rose-500">检查 Alpaca 凭据 / owner token。</div>
      </div>
    </div>
  );
  if (!data) return null;

  const { pnl, positions, history } = data;

  return (
    <div className="p-4 space-y-4 overflow-y-auto h-full">
      {/* Equity + intraday P&L */}
      <div className="rounded-xl bg-white shadow-sm border border-slate-200 p-4">
        <div className="flex items-baseline justify-between">
          <span className="text-slate-500 text-sm">总权益{pnl.paper ? " · 模拟盘" : ""}</span>
          <span className={`text-sm font-medium ${signColor(pnl.intraday_pl)}`}>
            当日 {money(pnl.intraday_pl)} ({pct(pnl.intraday_pl_pct)})
          </span>
        </div>
        <div className="text-3xl font-semibold text-slate-800 mt-1">{money(pnl.equity)}</div>
        <Sparkline values={history.equity} />
        <div className="grid grid-cols-3 gap-2 mt-3 text-xs text-slate-500">
          <div>现金<div className="text-slate-700 text-sm">{money(pnl.cash)}</div></div>
          <div>持仓数<div className="text-slate-700 text-sm">{pnl.position_count}</div></div>
          <div>多头市值<div className="text-slate-700 text-sm">{money(pnl.long_value)}</div></div>
        </div>
      </div>

      {/* Positions */}
      <div className="rounded-xl bg-white shadow-sm border border-slate-200 overflow-hidden">
        <div className="px-4 py-2 text-sm font-medium text-slate-600 border-b border-slate-100">
          持仓 ({positions.length})
        </div>
        {positions.length === 0 ? (
          <div className="p-4 text-sm text-slate-400">当前无持仓</div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-slate-400 text-xs">
                <th className="text-left font-normal px-4 py-1">代码</th>
                <th className="text-right font-normal px-2">市值</th>
                <th className="text-right font-normal px-4">浮动盈亏</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.symbol} className="border-t border-slate-50">
                  <td className="px-4 py-1.5 font-medium text-slate-700">{p.symbol}</td>
                  <td className="px-2 text-right text-slate-600">{money(p.market_value)}</td>
                  <td className={`px-4 text-right ${signColor(p.unrealized_pl)}`}>
                    {money(p.unrealized_pl)} <span className="text-xs">({pct(p.unrealized_pl_pct)})</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
