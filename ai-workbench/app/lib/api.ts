'use client';

/** Owner-token aware fetch helpers shared by the workbench pages. */

export function authHeaders(): Record<string, string> {
  const token = typeof window === 'undefined' ? '' : localStorage.getItem('ownerToken') || localStorage.getItem('dashboard:owner_token') || '';
  return token ? { 'X-Owner-Token': token } : {};
}

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail ? `${status}: ${detail}` : `${status}`);
    this.status = status;
    this.detail = detail;
  }
}

export async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { cache: 'no-store', ...init, headers: { 'Content-Type': 'application/json', ...authHeaders(), ...((init?.headers as Record<string, string>) || {}) } });
  if (!response.ok) {
    let detail = '';
    try { const body = await response.json() as { detail?: unknown }; detail = typeof body.detail === 'string' ? body.detail : body.detail ? JSON.stringify(body.detail) : '' } catch { /* non-JSON error body */ }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}
