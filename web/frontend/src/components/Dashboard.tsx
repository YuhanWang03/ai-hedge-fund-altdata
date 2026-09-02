import { useEffect, useState } from "react";
import { getFlowStatus, getPortfolio, getRisk } from "../api";
import type { FlowStatusResp, PortfolioResp, RiskResp } from "../types";
import EquityCurve from "./EquityCurve";
import KpiStrip from "./KpiStrip";
import PositionsTable from "./PositionsTable";
import RiskCard from "./RiskCard";
import Recommendations from "./Recommendations";
import TickerTape from "./TickerTape";

export default function Dashboard({
  refreshKey,
  recoKey,
  onPick,
}: {
  refreshKey: number;
  recoKey: number;
  onPick: (ticker: string) => void;
}) {
  const [pf, setPf] = useState<PortfolioResp | null>(null);
  const [risk, setRisk] = useState<RiskResp | null>(null);
  const [flow, setFlow] = useState<FlowStatusResp>({});
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    setErr("");
    getRisk().then((r) => alive && setRisk(r)).catch(() => {});
    getPortfolio()
      .then((d) => {
        if (!alive) return;
        setPf(d);
        const syms = d.positions.map((p) => p.symbol);
        if (syms.length) getFlowStatus(syms).then((f) => alive && setFlow(f)).catch(() => {});
      })
      .catch((e) => alive && setErr(String(e.message || e)));
    return () => { alive = false; };
  }, [refreshKey]);

  return (
    <div className="p-4 space-y-3 overflow-y-auto h-full">
      <TickerTape refreshKey={refreshKey} />
      <KpiStrip risk={risk} />
      <EquityCurve refreshKey={refreshKey} />
      <RiskCard risk={risk} />
      {err ? (
        <div className="rounded-lg bg-rose-50 border border-rose-200 p-3 text-sm text-rose-700">
          无法获取持仓：<code>{err}</code>
          <div className="mt-1 text-rose-500">检查 Alpaca 凭据 / owner token。</div>
        </div>
      ) : (
        <PositionsTable positions={pf?.positions || []} flow={flow} onPick={onPick} />
      )}
      <Recommendations refreshKey={recoKey} onPick={onPick} />
    </div>
  );
}
