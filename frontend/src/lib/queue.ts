// Queue sorting / grouping / filtering — pure, UI-agnostic helpers so the Review Queue can be
// reorganized instantly client-side (no API round-trip per sort) and the logic stays testable.
//
// Job-marketing rationale baked in:
//  • Freshness is the #1 callback lever — odds fall sharply after the first week, so the default
//    "Recommended" order decays a posting's weight over ~2 weeks.
//  • Fit/quality is the #2 lever — a reviewed, tailored app converts far better than a generic one.
//  • Readiness (résumé already tailored) means you can apply NOW with zero wait — worth surfacing.
//  • Batching by Company or ATS lets you knock out many forms in one sitting (extension Fill-All,
//    same form muscle memory) — pure throughput.
import type { Application } from "@/lib/api";

export type SortKey = "recommended" | "fresh" | "fit" | "company" | "added";
export type GroupKey = "none" | "company" | "ats" | "freshness";

export const SORT_OPTIONS: { key: SortKey; label: string; hint: string }[] = [
  { key: "recommended", label: "Recommended", hint: "Highest expected value — blends fit, freshness, and readiness" },
  { key: "fresh",       label: "Freshest",       hint: "Most recently posted first — the strongest callback edge" },
  { key: "fit",         label: "Best fit",       hint: "Highest reviewed quality / fit score first" },
  { key: "company",     label: "Company A–Z",    hint: "Alphabetical — naturally batches roles at one company" },
  { key: "added",       label: "Recently added", hint: "Newest into the queue first" },
];

export const GROUP_OPTIONS: { key: GroupKey; label: string }[] = [
  { key: "none",      label: "No grouping" },
  { key: "company",   label: "By company" },
  { key: "freshness", label: "By freshness" },
];

export function atsOf(url: string | null | undefined): string {
  const u = (url || "").toLowerCase();
  if (!u) return "Other";
  if (u.includes("greenhouse")) return "Greenhouse";
  if (u.includes("lever.co")) return "Lever";
  if (u.includes("ashby")) return "Ashby";
  if (u.includes("workday") || u.includes("myworkdayjobs")) return "Workday";
  if (u.includes("smartrecruiters")) return "SmartRecruiters";
  if (u.includes("workable")) return "Workable";
  if (u.includes("icims")) return "iCIMS";
  if (u.includes("jobvite")) return "Jobvite";
  if (u.includes("teksystems")) return "TEKsystems";
  if (u.includes("kforce")) return "Kforce";
  if (u.includes("greenhouse.io/embed")) return "Greenhouse";
  return "Other";
}

export function daysOld(postedAt: string | null | undefined): number | null {
  if (!postedAt) return null;
  const t = new Date(postedAt).getTime();
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.floor((Date.now() - t) / 86_400_000));
}

/** Minutes since the app entered the queue (created_at), with second precision. null if unknown. */
export function minutesSince(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.max(0, (Date.now() - t) / 60_000);
}

// "Added within" windows (minutes) for catching freshly-discovered roles fast.
export const ADDED_WINDOWS: { key: string; label: string; minutes: number }[] = [
  { key: "5m",  label: "5 min",  minutes: 5 },
  { key: "15m", label: "15 min", minutes: 15 },
  { key: "30m", label: "30 min", minutes: 30 },
  { key: "1h",  label: "1 hour", minutes: 60 },
  { key: "2h",  label: "2 hours", minutes: 120 },
  { key: "3h",  label: "3 hours", minutes: 180 },
  { key: "4h",  label: "4 hours", minutes: 240 },
  { key: "5h",  label: "5 hours", minutes: 300 },
  { key: "12h", label: "12 hours", minutes: 720 },
  { key: "24h", label: "24 hours", minutes: 1440 },
];

/** Compact "added 4m ago" / "added 2h ago" / "added 3d ago" from created_at. */
export function addedAgo(iso: string | null | undefined): string | null {
  const m = minutesSince(iso);
  if (m === null) return null;
  if (m < 1) return "just now";
  if (m < 60) return `${Math.floor(m)}m ago`;
  if (m < 1440) return `${Math.floor(m / 60)}h ago`;
  return `${Math.floor(m / 1440)}d ago`;
}

export function freshnessBucket(d: number | null): string {
  if (d === null) return "Unknown date";
  if (d === 0) return "Today";
  if (d <= 7) return "This week";
  if (d <= 21) return "2–3 weeks";
  return "Older";
}

export function scoreOf(a: Application): number {
  return a.quality_score ?? a.fit_score ?? 0;
}

export function isPrepared(a: Application): boolean {
  return a.quality_score != null;
}

/** A high-fit role flagged for a warm intro that the user hasn't acted on yet — surface it first. */
export function isReferralFirst(a: Application): boolean {
  return !!a.referral_first && !a.referral_done;
}

/** Expected-value blend used by the default "Recommended" sort. */
export function recommendedScore(a: Application): number {
  const fit = (a.quality_score ?? a.fit_score ?? 50) / 100;     // 0..1
  const d = daysOld(a.posted_at);
  const fresh = d === null ? 0.4 : Math.exp(-d / 14);           // freshness decays over ~2 weeks
  const prepared = isPrepared(a) ? 1 : 0;                       // résumé already tailored → apply now
  const referral = isReferralFirst(a) ? 0.15 : 0;              // warm-intro roles bubble to the top
  return 0.45 * fit + 0.35 * fresh + 0.2 * prepared + referral;
}

const tie = (a: Application, b: Application) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0); // stable

export function sortApps(apps: Application[], key: SortKey): Application[] {
  const cmp: Record<SortKey, (a: Application, b: Application) => number> = {
    recommended: (a, b) => recommendedScore(b) - recommendedScore(a) || tie(a, b),
    fresh:       (a, b) => (daysOld(a.posted_at) ?? 1e9) - (daysOld(b.posted_at) ?? 1e9) || tie(a, b),
    fit:         (a, b) => scoreOf(b) - scoreOf(a) || tie(a, b),
    company:     (a, b) => (a.company_name || "").localeCompare(b.company_name || "") || tie(a, b),
    // "Recently added" = when it ENTERED the queue (created_at), not last-touched (updated_at).
    added:       (a, b) => ((b.created_at || b.updated_at) || "").localeCompare((a.created_at || a.updated_at) || "") || tie(a, b),
  };
  return [...apps].sort(cmp[key]);
}

export interface QueueFilters {
  search: string;
  freshOnly: boolean;     // posted within 7 days
  preparedOnly: boolean;  // résumé already tailored
  ats: string;            // "" = any, else a specific ATS label
  addedWithinMin: number; // 0 = any; else only apps that entered the queue within N minutes
}

export function filterApps(apps: Application[], f: QueueFilters): Application[] {
  const q = f.search.trim().toLowerCase();
  return apps.filter((a) => {
    if (q && !(`${a.company_name ?? ""} ${a.role_title ?? ""}`.toLowerCase().includes(q))) return false;
    if (f.freshOnly) { const d = daysOld(a.posted_at); if (d === null || d > 7) return false; }
    if (f.preparedOnly && !isPrepared(a)) return false;
    if (f.ats && atsOf(a.ats_url) !== f.ats) return false;
    if (f.addedWithinMin > 0) { const m = minutesSince(a.created_at); if (m === null || m > f.addedWithinMin) return false; }
    return true;
  });
}

export interface QueueGroup { label: string; items: Application[]; avgScore: number | null }

export function groupApps(apps: Application[], key: GroupKey): QueueGroup[] {
  const withMeta = (label: string, items: Application[]): QueueGroup => {
    const scored = items.map(scoreOf).filter((s) => s > 0);
    return { label, items, avgScore: scored.length ? Math.round(scored.reduce((x, y) => x + y, 0) / scored.length) : null };
  };
  if (key === "none") return [withMeta("", apps)];
  const keyFn = (a: Application) =>
    key === "company" ? (a.company_name || "Unknown")
    : key === "ats" ? atsOf(a.ats_url)
    : freshnessBucket(daysOld(a.posted_at));
  const map = new Map<string, Application[]>();
  for (const a of apps) {
    const k = keyFn(a);
    if (!map.has(k)) map.set(k, []);
    map.get(k)!.push(a);
  }
  const groups = [...map.entries()].map(([label, items]) => withMeta(label, items));
  if (key === "freshness") {
    const order = ["Today", "This week", "2–3 weeks", "Older", "Unknown date"];
    groups.sort((a, b) => order.indexOf(a.label) - order.indexOf(b.label));
  } else {
    groups.sort((a, b) => b.items.length - a.items.length || a.label.localeCompare(b.label));
  }
  return groups;
}

export function distinctAts(apps: Application[]): string[] {
  return [...new Set(apps.map((a) => atsOf(a.ats_url)))].sort();
}
