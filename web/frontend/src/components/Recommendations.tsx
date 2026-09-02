import { useEffect, useState } from "react";
import { getRecommendations } from "../api";
import { num } from "../format";
import type { Recommendation } from "../types";

export default function Recommendations({
  refreshKey,
  onPick,
}: {
  refreshKey: number;
  onPick: (ticker: string) => void;
}) {
  const [items, setItems] = useState<Recommendation[] | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    setErr("");
    // Keep the old list visible while re-scanning (no flicker on auto-refresh).
    getRecommendations()
      .then((d) => alive && setItems(d.items))
      .catch((e) => alive && setErr(String(e.message || e)));
    return () => { alive = false; };
  }, [refreshKey]);

  return (
    <div className="rounded-xl bg-white border border-slate-200 overflow-hidden">
      <div className="px-4 py-2 text-sm font-medium text-slate-600 border-b border-slate-100 flex items-center justify-between">
        <span>推荐关注 · 资金流吸筹榜</span>
        <span className="text-[10px] text-slate-400">TECH_30 扫描</span>
      </div>
      {err ? (
        <div className="p-4 text-sm text-slate-400">扫描不可用</div>
      ) : items == null ? (
        <div className="p-4 text-sm text-slate-400">扫描全市场吸筹信号中…（约 10 秒）</div>
      ) : items.length === 0 ? (
        <div className="p-4 text-sm text-slate-400">当前无明显吸筹信号</div>
      ) : (
        <table className="w-full text-sm">
          <tbody>
            {items.map((r) => (
              <tr key={r.ticker}
                className="border-t border-slate-50 hover:bg-slate-50 cursor-pointer"
                onClick={() => onPick(r.ticker)}>
                <td className="px-4 py-1.5 font-medium text-slate-700">{r.ticker}</td>
                <td className="px-2">
                  <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                    r.strength === "strong" ? "bg-emerald-100 text-emerald-700" : "bg-emerald-50 text-emerald-600"
                  }`}>
                    吸筹{r.strength === "strong" ? "·强" : ""}
                  </span>
                </td>
                <td className="px-2 text-right text-xs text-slate-500">
                  CMF {r.cmf >= 0 ? "+" : ""}{num(r.cmf, 2)}
                </td>
                <td className="px-4 text-right text-xs text-slate-500">RSI {num(r.rsi, 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="px-4 py-1.5 text-[10px] text-slate-400 border-t border-slate-50">
        量价代理信号,非买入建议;点击看 /flow 详情
      </div>
    </div>
  );
}
