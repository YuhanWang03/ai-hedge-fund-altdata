import { money, pct, signColor } from "../format";
import type { RiskResp } from "../types";

function Kpi({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="flex-1 min-w-[110px] rounded-xl bg-white border border-slate-200 px-3 py-2">
      <div className="text-[11px] text-slate-400">{label}</div>
      <div className={`text-lg font-semibold ${color || "text-slate-800"}`}>{value}</div>
    </div>
  );
}

export default function KpiStrip({ risk }: { risk: RiskResp | null }) {
  if (!risk) return null;
  const { pnl } = risk;
  return (
    <div className="flex gap-2 flex-wrap">
      <Kpi label="总权益" value={money(risk.portfolio_value)} />
      <Kpi label="当日" value={pnl.daily_pnl_pct != null ? pct(pnl.daily_pnl_pct) : "—"} color={signColor(pnl.daily_pnl_pct)} />
      <Kpi label="本周" value={pnl.weekly_pnl_pct != null ? pct(pnl.weekly_pnl_pct) : "—"} color={signColor(pnl.weekly_pnl_pct)} />
      <Kpi label="本月" value={pnl.monthly_pnl_pct != null ? pct(pnl.monthly_pnl_pct) : "—"} color={signColor(pnl.monthly_pnl_pct)} />
      <Kpi label="现金" value={money(risk.cash)} />
    </div>
  );
}
