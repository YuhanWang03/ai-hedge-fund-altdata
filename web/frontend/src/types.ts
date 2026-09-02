export interface Position {
  symbol: string;
  qty: number;
  avg_entry_price: number;
  current_price: number;
  market_value: number;
  unrealized_pl: number;
  unrealized_pl_pct: number;
  side: string;
}

export interface Pnl {
  date: string;
  paper: boolean;
  equity: number;
  last_equity: number;
  intraday_pl: number;
  intraday_pl_pct: number;
  cash: number;
  portfolio_value: number;
  buying_power: number;
  position_count: number;
  long_value: number;
  short_value: number;
}

export interface PortfolioResp {
  account: {
    cash: number;
    portfolio_value: number;
    buying_power: number;
    status: string;
    paper: boolean;
  };
  positions: Position[];
  pnl: Pnl;
  history: { timestamp: number[]; equity: number[] };
}

export interface ChatResp {
  intent: string;
  html: string;
  chart_b64?: string;
  extra_html?: string[];
}

export interface RiskResp {
  portfolio_value: number;
  cash: number;
  cash_pct: number;
  invested_value: number;
  pnl: {
    daily_pnl: number | null;
    daily_pnl_pct: number | null;
    weekly_pnl_pct: number | null;
    monthly_pnl_pct: number | null;
  };
  concentration: { top_1_pct: number; top_3_pct: number; hhi: number; n_positions: number };
  exposure: { by_sector: Record<string, number>; largest_sector: string; largest_sector_pct: number };
  drawdown: { current_drawdown_pct: number | null; max_drawdown_pct: number | null };
  earnings_risk: { ticker: string; release_date: string; days_until: number }[];
  warnings: string[];
}

export interface MacroResp {
  vix: number | null;
  vix_pct_change_1d: number | null;
  dxy: number | null;
  wti_crude: number | null;
  gold: number | null;
  dgs2: number | null;
  dgs10: number | null;
  t10y2y: number | null;
  fed_funds_upper: number | null;
  vix_spike: boolean;
  curve_flip: boolean;
  rates_shocked: boolean;
  warnings: string[];
}

export interface HistoryResp {
  period: string;
  timestamp: number[];
  equity: number[];
}

export type FlowState = "accumulation" | "distribution" | "none" | "unknown";
export interface FlowStatus {
  state: FlowState;
  strength: string | null;
  cmf: number | null;
  rsi: number | null;
}
export type FlowStatusResp = Record<string, FlowStatus>;

export interface TickerItem {
  label: string;
  value: number;
  change_pct: number | null;
  unit: string;
}
export interface TickerTapeResp {
  items: TickerItem[];
}

export interface Recommendation {
  ticker: string;
  strength: string;
  cmf: number;
  rsi: number;
  rsi_divergence: string;
}
export interface RecommendationsResp {
  items: Recommendation[];
}
