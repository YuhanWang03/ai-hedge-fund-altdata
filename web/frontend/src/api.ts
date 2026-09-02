import type { ChatResp, PortfolioResp } from "./types";

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

export function postChat(text: string): Promise<ChatResp> {
  return req<ChatResp>("/api/chat", {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}
