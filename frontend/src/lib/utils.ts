import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return "never";
  const diff = Date.now() - new Date(iso).getTime();
  const s = Math.floor(diff / 1000);
  if (s < 60)    return `${s}s ago`;
  if (s < 3600)  return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export const STATUS_COLOR: Record<string, string> = {
  submitted:             "bg-green-500/20 text-green-400 border-green-500/40",
  submitting:            "bg-teal-500/20 text-teal-400 border-teal-500/40",
  pending_approval:      "bg-yellow-500/20 text-yellow-400 border-yellow-500/40",
  pending_manual_submit: "bg-orange-500/20 text-orange-400 border-orange-500/40",
  blocked:               "bg-red-500/20 text-red-400 border-red-500/40",
  skipped:               "bg-slate-500/20 text-slate-400 border-slate-500/40",
  approved:              "bg-blue-500/20 text-blue-400 border-blue-500/40",
  queued:                "bg-slate-600/20 text-slate-400 border-slate-600/40",
  tailoring:             "bg-purple-500/20 text-purple-400 border-purple-500/40",
  scoring:               "bg-indigo-500/20 text-indigo-400 border-indigo-500/40",
};

export const STATUS_LABEL: Record<string, string> = {
  submitted:             "Submitted",
  submitting:            "Awaiting Email",
  pending_approval:      "Pending Review",
  pending_manual_submit: "Manual Submit",
  blocked:               "Blocked",
  skipped:               "Skipped",
  approved:              "Approved",
  queued:                "Queued",
  tailoring:             "Tailoring",
  scoring:               "Scoring",
};
