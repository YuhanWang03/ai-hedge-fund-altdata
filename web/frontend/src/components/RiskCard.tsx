import { pct } from "../format";
import type { RiskResp } from "../types";

function Bar({ label, value, max = 1 }: { label: string; value: number; max?: number }) {
  const w = Math.max(0, Math.min(100, (value / max) * 100));
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="w-14 text-slate-500 shrink-0">{label}</span>
      <div className="flex-1 h-2 rounded bg-slate-100 overflow-hidden">
        <div className="h-full bg-blue-400" style={{ width: `${w}%` }} />
      </div>
      <span className="w-12 text-right text-slate-600">{(value * 100).toFixed(0)}%</span>
    </div>
  );
}

export default function RiskCard({ risk }: { risk: RiskResp | null }) {
  if (!risk) return null;
  const { concentration: c, exposure: e, drawdown: d } = risk;
  const sectors = Object.entries(e.by_sector).sort((a, b) => b[1] - a[1]).slice(0, 4);

  return (
    <div className="rounded-xl bg-white border border-slate-200 p-3 space-y-3">
      <div className="text-sm font-medium text-slate-600">风险</div>

      <div className="grid grid-cols-3 gap-2 text-center">
        <div>
          <div className="text-[11px] text-slate-400">最大单一</div>
          <div className={`text-base font-semibold ${c.top_1_pct >= 0.3 ? "text-rose-600" : "text-slate-700"}`}>
            {(c.top_1_pct * 100).toFixed(0)}%
          </div>
        </div>
        <div>
          <div className="text-[11px] text-slate-400">前三持仓</div>
          <div className="text-base font-semibold text-slate-700">{(c.top_3_pct * 100).toFixed(0)}%</div>
        </div>
        <div>
          <div className="text-[11px] text-slate-400">最大回撤</div>
          <div className="text-base font-semibold text-rose-600">
            {d.max_drawdown_pct != null ? `-${(d.max_drawdown_pct * 100).toFixed(1)}%` : "—"}
          </div>
        </div>
      </div>

      {sectors.length > 0 && (
        <div className="space-y-1">
          <div className="text-[11px] text-slate-400">行业暴露</div>
          {sectors.map(([s, v]) => (
            <Bar key={s} label={s} value={v} />
          ))}
        </div>
      )}

      {risk.earnings_risk.length > 0 && (
        <div className="text-xs text-slate-500">
          未来 7 天财报：
          {risk.earnings_risk.map((e2) => (
            <span key={e2.ticker} className="ml-1 px-1.5 py-0.5 rounded bg-amber-50 text-amber-700">
              {e2.ticker} D-{e2.days_until}
            </span>
          ))}
        </div>
      )}

      {risk.pnl.daily_pnl_pct != null && risk.pnl.daily_pnl_pct <= -0.05 && (
        <div className="text-xs text-rose-600">⚠️ 当日回撤较大 ({pct(risk.pnl.daily_pnl_pct)})</div>
      )}
    </div>
  );
}
