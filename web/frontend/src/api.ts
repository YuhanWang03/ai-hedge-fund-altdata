import type {
  ChatResp,
  FlowStatusResp,
  HistoryResp,
  MacroResp,
  PortfolioResp,
  RecommendationsResp,
  RiskResp,
  TickerTapeResp,
} from "./types";

const TOKEN_KEY = "ownerToken";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setToken(t: string): void {
  localStorage.setItem(TOKEN_KEY, t);
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-Owner-Token": getToken(),
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status}: ${body.slice(0, 300)}`);
  }
  return res.json() as Promise<T>;
}

export function getPortfolio(): Promise<PortfolioResp> {
  return req<PortfolioResp>("/api/portfolio");
}

export function getRisk(): Promise<RiskResp> {
  return req<RiskResp>("/api/risk");
}

export function getMacro(): Promise<MacroResp> {
  return req<MacroResp>("/api/macro");
}

export function getHistory(period: string): Promise<HistoryResp> {
  return req<HistoryResp>(`/api/history?period=${encodeURIComponent(period)}`);
}

export function getFlowStatus(tickers: string[]): Promise<FlowStatusResp> {
  return req<FlowStatusResp>(`/api/flow_status?tickers=${encodeURIComponent(tickers.join(","))}`);
}

export function getTickerTape(): Promise<TickerTapeResp> {
  return req<TickerTapeResp>("/api/tickertape");
}

export function getRecommendations(): Promise<RecommendationsResp> {
  return req<RecommendationsResp>("/api/recommendations");
}

export function postChat(text: string): Promise<ChatResp> {
  return req<ChatResp>("/api/chat", {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}
