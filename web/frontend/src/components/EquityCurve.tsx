import { useEffect, useState } from "react";
import { getHistory } from "../api";
import { money } from "../format";
import type { HistoryResp } from "../types";

const PERIODS: { key: string; label: string }[] = [
  { key: "1M", label: "1M" },
  { key: "3M", label: "3M" },
  { key: "1A", label: "1Y" },
];

function Chart({ equity }: { equity: number[] }) {
  if (equity.length < 2) return <div className="text-xs text-slate-400 py-8 text-center">数据不足</div>;
  const w = 560, h = 160, pad = 4;
  const min = Math.min(...equity), max = Math.max(...equity);
  const span = max - min || 1;
  const x = (i: number) => pad + (i / (equity.length - 1)) * (w - 2 * pad);
  const y = (v: number) => pad + (1 - (v - min) / span) * (h - 2 * pad);
  const pts = equity.map((v, i) => `${x(i)},${y(v)}`).join(" ");
  const area = `${pad},${h - pad} ${pts} ${w - pad},${h - pad}`;
  const up = equity[equity.length - 1] >= equity[0];
  const stroke = up ? "#059669" : "#e11d48";
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full" preserveAspectRatio="none" style={{ height: 160 }}>
      <polygon points={area} fill={stroke} opacity={0.08} />
      <polyline points={pts} fill="none" stroke={stroke} strokeWidth={2} />
    </svg>
  );
}

export default function EquityCurve({ refreshKey }: { refreshKey: number }) {
  const [period, setPeriod] = useState("1M");
  const [data, setData] = useState<HistoryResp | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    setErr("");
    getHistory(period).then((d) => alive && setData(d)).catch((e) => alive && setErr(String(e.message || e)));
    return () => { alive = false; };
  }, [period, refreshKey]);

  const eq = data?.equity || [];
  const last = eq.length ? eq[eq.length - 1] : null;
  const first = eq.length ? eq[0] : null;
  const change = last != null && first != null && first > 0 ? (last - first) / first : null;

  return (
    <div className="rounded-xl bg-white border border-slate-200 p-3">
      <div className="flex items-center justify-between mb-1">
        <div className="text-sm text-slate-500">
          净值曲线{change != null && (
            <span className={change >= 0 ? "text-emerald-600 ml-2" : "text-rose-600 ml-2"}>
              {change >= 0 ? "+" : ""}{(change * 100).toFixed(1)}%
            </span>
          )}
        </div>
        <div className="flex gap-1">
          {PERIODS.map((p) => (
            <button key={p.key} onClick={() => setPeriod(p.key)}
              className={`text-xs px-2 py-0.5 rounded ${
                period === p.key ? "bg-slate-800 text-white" : "text-slate-500 hover:bg-slate-100"
              }`}>
              {p.label}
            </button>
          ))}
        </div>
      </div>
      {err ? (
        <div className="text-xs text-slate-400 py-8 text-center">曲线数据不可用</div>
      ) : (
        <>
          <Chart equity={eq} />
          {last != null && <div className="text-right text-xs text-slate-400 mt-1">{money(last)}</div>}
        </>
      )}
    </div>
  );
}
