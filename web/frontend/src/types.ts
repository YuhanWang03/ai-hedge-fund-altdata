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
