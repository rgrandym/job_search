import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export const cn = (...inputs: ClassValue[]) => twMerge(clsx(inputs));

export const scoreColor = (score: number) =>
  score >= 85 ? "var(--good)" : score >= 70 ? "var(--accent)" : score >= 50 ? "var(--warn)" : "var(--bad)";

export const money = (n: number | null | undefined) =>
  n == null ? "" : n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
