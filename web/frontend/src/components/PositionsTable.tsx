import { money, pct, signColor } from "../format";
import type { FlowStatusResp, Position } from "../types";

function FlowBadge({ status }: { status?: { state: string; strength: string | null } }) {
  if (!status || status.state === "none" || status.state === "unknown") {
    return <span className="text-[10px] text-slate-300">—</span>;
  }
  const acc = status.state === "accumulation";
  const label = acc ? "吸筹" : "派发";
  const cls = acc ? "bg-emerald-50 text-emerald-700" : "bg-rose-50 text-rose-700";
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded ${cls}`}>
      {label}{status.strength === "strong" ? "·强" : ""}
    </span>
  );
}

export default function PositionsTable({
  positions,
  flow,
  onPick,
}: {
  positions: Position[];
  flow: FlowStatusResp;
  onPick?: (ticker: string) => void;
}) {
  return (
    <div className="rounded-xl bg-white border border-slate-200 overflow-hidden">
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
              <th className="text-left font-normal px-1">资金流</th>
              <th className="text-right font-normal px-2">市值</th>
              <th className="text-right font-normal px-4">浮动盈亏</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr key={p.symbol}
                className="border-t border-slate-50 hover:bg-slate-50 cursor-pointer"
                onClick={() => onPick?.(p.symbol)}>
                <td className="px-4 py-1.5 font-medium text-slate-700">{p.symbol}</td>
                <td className="px-1"><FlowBadge status={flow[p.symbol]} /></td>
                <td className="px-2 text-right text-slate-600">{money(p.market_value)}</td>
                <td className={`px-4 text-right ${signColor(p.unrealized_pl)}`}>
                  {money(p.unrealized_pl)} <span className="text-xs">({pct(p.unrealized_pl_pct)})</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {positions.length > 0 && (
        <div className="px-4 py-1.5 text-[10px] text-slate-400 border-t border-slate-50">
          点击某只 → 右侧聊天区展开 /flow 图表
        </div>
      )}
    </div>
  );
}
