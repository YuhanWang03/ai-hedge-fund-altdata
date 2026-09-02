export const money = (n: number | null | undefined, dp = 0) =>
  n == null
    ? "—"
    : n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: dp });

export const pct = (n: number | null | undefined) =>
  n == null ? "—" : `${n >= 0 ? "+" : ""}${(n * 100).toFixed(2)}%`;

export const num = (n: number | null | undefined, dp = 2) =>
  n == null ? "—" : n.toFixed(dp);

export const signColor = (n: number | null | undefined) =>
  n == null || n === 0 ? "text-slate-500" : n > 0 ? "text-emerald-600" : "text-rose-600";
