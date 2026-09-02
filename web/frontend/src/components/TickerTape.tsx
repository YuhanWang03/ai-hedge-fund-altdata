import { useEffect, useState } from "react";
import { getTickerTape } from "../api";
import type { TickerItem } from "../types";

function fmtVal(v: number): string {
  return v >= 1000
    ? v.toLocaleString("en-US", { maximumFractionDigits: 0 })
    : v.toFixed(2);
}

function Item({ it }: { it: TickerItem }) {
  const c = it.change_pct;
  const color = c == null ? "text-slate-400" : c >= 0 ? "text-emerald-600" : "text-rose-600";
  return (
    <span className="inline-flex items-center gap-1.5 px-4 border-r border-slate-100">
      <span className="text-slate-500 text-xs">{it.label}</span>
      <span className="text-slate-800 text-sm font-medium">{fmtVal(it.value)}{it.unit}</span>
      {c != null && (
        <span className={`text-xs ${color}`}>{c >= 0 ? "+" : ""}{(c * 100).toFixed(2)}%</span>
      )}
    </span>
  );
}

export default function TickerTape({ refreshKey }: { refreshKey: number }) {
  const [items, setItems] = useState<TickerItem[]>([]);

  useEffect(() => {
    let alive = true;
    getTickerTape().then((d) => alive && setItems(d.items)).catch(() => {});
    return () => { alive = false; };
  }, [refreshKey]);

  if (items.length === 0) {
    return <div className="text-xs text-slate-400 px-1 py-1.5">加载行情条…</div>;
  }
  const doubled = [...items, ...items];
  return (
    <div className="ticker-mask overflow-hidden rounded-lg bg-white border border-slate-200 py-1.5">
      <div className="ticker-track">
        {doubled.map((it, i) => (
          <Item key={i} it={it} />
        ))}
      </div>
    </div>
  );
}
