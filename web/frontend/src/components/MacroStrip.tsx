import { useEffect, useState } from "react";
import { getMacro } from "../api";
import { num, pct } from "../format";
import type { MacroResp } from "../types";

function Cell({ label, value, sub, warn }: { label: string; value: string; sub?: string; warn?: boolean }) {
  return (
    <div className={`px-3 py-1.5 rounded-lg ${warn ? "bg-amber-50" : "bg-white"} border border-slate-200`}>
      <div className="text-[10px] text-slate-400">{label}</div>
      <div className="text-sm font-medium text-slate-700">{value}</div>
      {sub && <div className="text-[10px] text-slate-400">{sub}</div>}
    </div>
  );
}

export default function MacroStrip({ refreshKey }: { refreshKey: number }) {
  const [m, setM] = useState<MacroResp | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    getMacro().then((d) => alive && setM(d)).catch((e) => alive && setErr(String(e.message || e)));
    return () => { alive = false; };
  }, [refreshKey]);

  if (err) return <div className="text-xs text-slate-400 px-1">宏观数据不可用</div>;
  if (!m) return <div className="text-xs text-slate-400 px-1">加载市场环境…</div>;

  return (
    <div className="flex gap-2 flex-wrap">
      <Cell label="VIX" value={num(m.vix, 1)}
        sub={m.vix_pct_change_1d != null ? pct(m.vix_pct_change_1d) : undefined}
        warn={m.vix_spike} />
      <Cell label="10Y" value={m.dgs10 != null ? `${num(m.dgs10, 2)}%` : "—"} warn={m.rates_shocked} />
      <Cell label="2Y" value={m.dgs2 != null ? `${num(m.dgs2, 2)}%` : "—"} />
      <Cell label="10Y-2Y" value={m.t10y2y != null ? `${num(m.t10y2y, 2)}%` : "—"} warn={m.curve_flip} />
      <Cell label="DXY" value={num(m.dxy, 2)} />
      <Cell label="原油" value={m.wti_crude != null ? `$${num(m.wti_crude, 1)}` : "—"} />
    </div>
  );
}
