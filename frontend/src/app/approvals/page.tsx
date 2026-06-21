"use client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api, type Application } from "@/lib/api";
import { fmtRelative, STATUS_LABEL } from "@/lib/utils";
import {
  Eye, Check, X, ExternalLink, Sparkles, Users, MessageSquareText,
  Search, ChevronRight, Building2, Layers,
} from "lucide-react";
import { ProofModal } from "@/components/ProofModal";
import { ReferralModal } from "@/components/ReferralModal";
import { FormAnswersModal } from "@/components/FormAnswersModal";
import { ScoreBadge } from "@/components/ScoreBadge";
import {
  SORT_OPTIONS, GROUP_OPTIONS, ADDED_WINDOWS, atsOf, daysOld, scoreOf, addedAgo,
  sortApps, filterApps, groupApps, distinctAts,
  type SortKey, type GroupKey,
} from "@/lib/queue";

const PENDING_KEY = ["apps-pending-approvals"];

// "posted Xd ago" — green within a week (best recency edge), amber up to 3 weeks.
function PostedBadge({ postedAt }: { postedAt: string | null }) {
  const days = daysOld(postedAt);
  if (days === null) return null;
  const tone = days <= 7 ? "bg-green-500/15 text-green-400 border-green-500/30"
             : days <= 21 ? "bg-amber-500/15 text-amber-400 border-amber-500/30"
             : "bg-slate-600/15 text-slate-500 border-slate-600/30";
  const label = days === 0 ? "posted today" : `posted ${days}d ago`;
  return <span className={`text-xs px-2 py-0.5 rounded-full border ${tone} hidden md:inline-block`}>{label}</span>;
}

function AtsBadge({ url }: { url: string | null }) {
  const ats = atsOf(url);
  if (ats === "Other") return null;
  return (
    <span className="text-[11px] px-1.5 py-0.5 rounded border border-slate-600/40 bg-slate-700/30 text-slate-400 hidden lg:inline-block">
      {ats}
    </span>
  );
}

export default function ApprovalsPage() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [referralApp, setReferralApp] = useState<Application | null>(null);
  const [answersApp, setAnswersApp] = useState<Application | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // ── Queue organization state ──────────────────────────────────────────────
  const [sortBy, setSortBy] = useState<SortKey>("recommended");
  const [groupBy, setGroupBy] = useState<GroupKey>("none");
  const [search, setSearch] = useState("");
  const [freshOnly, setFreshOnly] = useState(false);
  const [preparedOnly, setPreparedOnly] = useState(false);
  const [hideStale, setHideStale] = useState(false);
  const [addedWithinMin, setAddedWithinMin] = useState(0); // 0 = any; else minutes-since-added window
  const [atsFilter, setAtsFilter] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set()); // groups default COLLAPSED (folder view)
  const [focusedId, setFocusedId] = useState<string | null>(null);

  const qc = useQueryClient();

  const { data: pending, isLoading: pendingLoading } = useQuery({
    queryKey: PENDING_KEY,
    // Fetch the whole queue so sorting/grouping/filtering is instant + complete client-side.
    queryFn: () => api.listApps("status=pending_approval&sort_by=recency&limit=300"),
    refetchInterval: 15_000,
  });

  const { data: submitted } = useQuery({
    queryKey: ["apps-submitted-approvals"],
    queryFn: () => api.listApps("status=submitted&sort_by=score&limit=100"),
    refetchInterval: 15_000,
  });

  const { data: sent } = useQuery({
    queryKey: ["apps-sent-approvals"],
    queryFn: () => api.listApps("status=submitting&sort_by=score&limit=100"),
    refetchInterval: 10_000,
  });

  const decide = useMutation({
    mutationFn: ({ id, action, reason }: { id: string; action: "approve" | "reject"; reason?: string }) =>
      api.decideApp(id, action, reason),
    onMutate: async ({ id }) => {
      setActionError(null);
      await qc.cancelQueries({ queryKey: PENDING_KEY });
      const prev = qc.getQueryData<Application[]>(PENDING_KEY);
      qc.setQueryData<Application[]>(PENDING_KEY, (old) => (old ?? []).filter((a) => a.id !== id));
      return { prev };
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData(PENDING_KEY, ctx.prev);
      setActionError("Action failed — the application was NOT updated. Check the API and retry.");
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: PENDING_KEY });
      qc.invalidateQueries({ queryKey: ["apps-sent-approvals"] });
      qc.invalidateQueries({ queryKey: ["apps-submitted-approvals"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
  });

  const tailor = useMutation({
    mutationFn: (id: string) => api.tailorApp(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: PENDING_KEY }),
    onError: () => setActionError("Tailoring failed — check API credits/connection and retry."),
  });
  const tailoringId = tailor.isPending ? tailor.variables : null;

  const onApplied = (id: string) => decide.mutate({ id, action: "approve" });
  const onSkip = (id: string) => {
    const reason = window.prompt("Reason for skipping (optional):") ?? undefined;
    decide.mutate({ id, action: "reject", reason });
  };
  const openApply = (app: Application) => {
    if (app.ats_url) window.open(app.ats_url, "_blank", "noopener");
  };
  const busyId = decide.isPending ? decide.variables?.id : null;

  const [notice, setNotice] = useState<string | null>(null);
  const PX = /teksystems\.com/i; // PerimeterX — 403s normal tabs, needs the incognito flow; skip in bulk
  const openableOf = (items: Application[]) => items.filter((a) => a.ats_url && !PX.test(a.ats_url || ""));
  const OPEN_CAP = 10;
  // Open a group's apply pages as tabs — auto-preparing any un-tailored résumés FIRST so the résumé
  // attaches instantly per tab (no on-demand tailoring wait) → then click the extension's "Fill ALL".
  const openAll = async (items: Application[]) => {
    const openable = openableOf(items);
    const skippedPX = items.length - openable.length;
    const batch = openable.slice(0, OPEN_CAP);
    if (!batch.length) { setNotice("Nothing to bulk-open here (no direct apply URLs, or all need the incognito flow)."); return; }
    const unprep = batch.filter((a) => a.quality_score == null);
    const tail = (openable.length > OPEN_CAP ? ` (first ${OPEN_CAP} of ${openable.length}; repeat for the rest.)` : "")
      + (skippedPX ? ` ${skippedPX} skipped — PerimeterX; use “Reopen in incognito”.` : "");
    // One confirm covering the résumé-prep cost (if any) + the tab count.
    const msg = unprep.length
      ? `Prepare ${unprep.length} résumé${unprep.length === 1 ? "" : "s"} (~$0.11 each, Opus) so they attach instantly, then open ${batch.length} tab${batch.length === 1 ? "" : "s"}?`
      : (batch.length > 6 ? `Open ${batch.length} application tabs? Then click the extension's “Fill ALL”.` : "");
    if (msg && !window.confirm(msg)) return;
    // Auto-prepare the un-tailored ones FIRST, in parallel, so /resume is instant when each tab fills.
    if (unprep.length) {
      setNotice(`Preparing ${unprep.length} résumé${unprep.length === 1 ? "" : "s"} (~30s each)… tabs open when ready.`);
      await Promise.allSettled(unprep.map((a) => api.tailorApp(a.id)));
      qc.invalidateQueries({ queryKey: PENDING_KEY });
    }
    // Prefer the EXTENSION to open tabs (chrome.tabs.create — no pop-up blocker). The bridge content
    // script marks the page with data-ah-ext when the extension is installed.
    if (typeof document !== "undefined" && document.documentElement.getAttribute("data-ah-ext") === "1") {
      window.postMessage({ source: "ah-dashboard", type: "open_tabs", urls: batch.map((a) => a.ats_url) }, "*");
      setNotice(`Opening ${batch.length} tab${batch.length === 1 ? "" : "s"} via the extension — then click its “Fill ALL”.${tail}`);
      return;
    }
    // Fallback (extension not installed): window.open — subject to the pop-up blocker.
    let blocked = 0;
    for (const a of batch) { if (!window.open(a.ats_url as string, "_blank", "noopener")) blocked++; }
    const parts = [`Opened ${batch.length - blocked} tab${batch.length - blocked === 1 ? "" : "s"} — now click the extension's “Fill ALL”.`];
    if (blocked) parts.push(`${blocked} blocked by the pop-up blocker — allow pop-ups for this site (or reload the extension to open tabs directly).`);
    setNotice(parts.join(" ") + tail);
  };

  // Batch-tailor the un-prepared résumés in a group so Open-all → Fill-All has them ready.
  const PREP_CAP = 5;
  const prepareAll = async (items: Application[]) => {
    const unprepared = items.filter((a) => a.quality_score == null).slice(0, PREP_CAP);
    if (!unprepared.length) { setNotice("Every role in this group already has a tailored résumé."); return; }
    if (!window.confirm(`Tailor ${unprepared.length} résumé${unprepared.length === 1 ? "" : "s"} now? (~$0.11 each, Opus — takes ~30s each.)`)) return;
    setNotice(`Tailoring ${unprepared.length} résumé${unprepared.length === 1 ? "" : "s"}… each row gets a score as it finishes.`);
    await Promise.allSettled(unprepared.map((a) => api.tailorApp(a.id)));
    qc.invalidateQueries({ queryKey: PENDING_KEY });
    setNotice(`Tailoring complete for ${unprepared.length} role${unprepared.length === 1 ? "" : "s"}.`);
  };
  const unpreparedOf = (items: Application[]) => items.filter((a) => a.quality_score == null).length;

  // ── Derived view: filter → sort → group ────────────────────────────────────
  const all = useMemo(() => pending ?? [], [pending]);
  const filtered = useMemo(() => {
    let f = filterApps(all, { search, freshOnly, preparedOnly, ats: atsFilter, addedWithinMin });
    if (hideStale) f = f.filter((a) => { const d = daysOld(a.posted_at); return d === null || d <= 21; });
    return sortApps(f, sortBy);
  }, [all, search, freshOnly, preparedOnly, hideStale, addedWithinMin, atsFilter, sortBy]);
  const groups = useMemo(() => groupApps(filtered, groupBy), [filtered, groupBy]);
  const atsList = useMemo(() => distinctAts(all), [all]);
  const filtersActive = !!search || freshOnly || preparedOnly || hideStale || addedWithinMin > 0 || !!atsFilter;

  // Follow-up lane: submitted 3–30 days ago — old enough to follow up, not so old it's dead. A short
  // follow-up or (better) a referral here is the highest-leverage post-apply move.
  const followUps = useMemo(
    () => (submitted ?? [])
      .filter((a) => { const d = daysOld(a.submitted_at); return d !== null && d >= 3 && d <= 30; })
      .sort((a, b) => scoreOf(b) - scoreOf(a)),
    [submitted]);
  const followUpIds = useMemo(() => new Set(followUps.map((a) => a.id)), [followUps]);
  const submittedRest = useMemo(() => (submitted ?? []).filter((a) => !followUpIds.has(a.id)), [submitted, followUpIds]);

  const rowHandlers = {
    tailor, tailoringId, openApply, onApplied, onSkip, busyId,
    setReferralApp, setAnswersApp, setSelectedId,
  };

  // ── Keyboard navigation: j/k (or ↑/↓) move · o open · a applied · s skip ────
  const modalOpen = !!(selectedId || referralApp || answersApp);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (modalOpen || /^(INPUT|TEXTAREA|SELECT)$/.test(el?.tagName || "")) return;
      if (!filtered.length) return;
      const idx = Math.max(0, filtered.findIndex((a) => a.id === focusedId));
      const cur = filtered[idx];
      const go = (i: number) => {
        const t = filtered[Math.min(filtered.length - 1, Math.max(0, i))];
        if (t) { setFocusedId(t.id); document.getElementById(`app-${t.id}`)?.scrollIntoView({ block: "center", behavior: "smooth" }); }
      };
      if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); go(idx + 1); }
      else if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); go(idx - 1); }
      else if (!cur) { /* nothing focused yet */ }
      else if (e.key === "o") { openApply(cur); }
      else if (e.key === "a") { onApplied(cur.id); }
      else if (e.key === "s") { onSkip(cur.id); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [filtered, focusedId, modalOpen]); // eslint-disable-line react-hooks/exhaustive-deps

  const toggleGroup = (label: string) =>
    setExpanded((c) => {
      const n = new Set(c);
      if (n.has(label)) n.delete(label); else n.add(label);
      return n;
    });

  // Mark every role in a group as Applied (after you've actually submitted them).
  const appliedAll = (items: Application[], label: string) => {
    if (!items.length) return;
    if (!window.confirm(`Mark all ${items.length} role${items.length === 1 ? "" : "s"} at ${label || "this group"} as Applied? Do this only after you've actually submitted them.`)) return;
    items.forEach((a) => decide.mutate({ id: a.id, action: "approve" }));
  };

  return (
    <div>
      <div className="mb-5">
        <h1 className="text-2xl font-bold text-slate-100">Review Queue</h1>
        <p className="text-sm text-slate-500 mt-0.5">
          <strong className="text-blue-400">Open &amp; Apply ↗</strong> opens the application in a new tab — fill it with the
          extension, submit, then mark it <strong className="text-green-400">Applied</strong> or <strong className="text-red-400">Skip</strong>.
        </p>
      </div>

      {actionError && (
        <div className="mb-4 flex items-center justify-between bg-red-600/15 border border-red-500/40 text-red-300 text-sm px-4 py-2.5 rounded-lg">
          <span>{actionError}</span>
          <button onClick={() => setActionError(null)} className="text-red-400 hover:text-red-200">✕</button>
        </div>
      )}
      {notice && (
        <div className="mb-4 flex items-center justify-between bg-blue-600/15 border border-blue-500/40 text-blue-200 text-sm px-4 py-2.5 rounded-lg">
          <span>{notice}</span>
          <button onClick={() => setNotice(null)} className="text-blue-300 hover:text-blue-100">✕</button>
        </div>
      )}

      {/* ── Queue cockpit: sort / group / filter ─────────────────────────── */}
      <div className="sticky top-0 z-20 -mx-2 px-2 py-3 mb-4 bg-slate-900/80 backdrop-blur border-b border-slate-800">
        <div className="flex flex-wrap items-center gap-2">
          {/* search */}
          <div className="relative flex-1 min-w-[180px] max-w-xs">
            <Search className="w-4 h-4 text-slate-500 absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search company or role…"
              className="w-full bg-slate-800/60 border border-slate-700 rounded-lg pl-8 pr-3 py-1.5 text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:border-blue-500/60"
            />
          </div>

          {/* sort pills */}
          <div className="flex items-center gap-1 bg-slate-800/60 border border-slate-700 rounded-lg p-0.5">
            {SORT_OPTIONS.map((o) => (
              <button
                key={o.key}
                onClick={() => setSortBy(o.key)}
                title={o.hint}
                className={`text-xs font-medium px-2.5 py-1 rounded-md transition-colors ${
                  sortBy === o.key ? "bg-blue-600 text-white" : "text-slate-400 hover:text-slate-200"
                }`}
              >
                {o.label}
              </button>
            ))}
          </div>

          {/* group select */}
          <div className="flex items-center gap-1.5 bg-slate-800/60 border border-slate-700 rounded-lg px-2 py-1.5">
            <Layers className="w-3.5 h-3.5 text-slate-500" />
            <select
              value={groupBy}
              onChange={(e) => setGroupBy(e.target.value as GroupKey)}
              className="bg-transparent text-xs text-slate-300 focus:outline-none cursor-pointer"
            >
              {GROUP_OPTIONS.map((g) => <option key={g.key} value={g.key} className="bg-slate-800">{g.label}</option>)}
            </select>
          </div>

          {/* filter chips */}
          <button
            onClick={() => setFreshOnly((v) => !v)}
            className={`text-xs font-medium px-2.5 py-1.5 rounded-lg border transition-colors ${
              freshOnly ? "bg-green-600/25 border-green-500/50 text-green-300" : "bg-slate-800/60 border-slate-700 text-slate-400 hover:text-slate-200"
            }`}
          >
            Fresh ≤7d
          </button>
          <button
            onClick={() => setPreparedOnly((v) => !v)}
            title="Résumé already tailored — apply right now, no wait"
            className={`text-xs font-medium px-2.5 py-1.5 rounded-lg border transition-colors ${
              preparedOnly ? "bg-purple-600/25 border-purple-500/50 text-purple-300" : "bg-slate-800/60 border-slate-700 text-slate-400 hover:text-slate-200"
            }`}
          >
            Prepared
          </button>
          <button
            onClick={() => setHideStale((v) => !v)}
            title="Hide postings older than 3 weeks — callback odds there are near zero"
            className={`text-xs font-medium px-2.5 py-1.5 rounded-lg border transition-colors ${
              hideStale ? "bg-orange-600/25 border-orange-500/50 text-orange-300" : "bg-slate-800/60 border-slate-700 text-slate-400 hover:text-slate-200"
            }`}
          >
            Hide stale
          </button>
          <select
            value={addedWithinMin}
            onChange={(e) => setAddedWithinMin(Number(e.target.value))}
            title="Show only roles that entered the queue within this window — catch fresh discoveries fast"
            className={`text-xs rounded-lg border px-2 py-1.5 focus:outline-none cursor-pointer ${
              addedWithinMin > 0 ? "bg-green-600/25 border-green-500/50 text-green-300" : "bg-slate-800/60 border-slate-700 text-slate-400"
            }`}
          >
            <option value={0} className="bg-slate-800">Added: any time</option>
            {ADDED_WINDOWS.map((w) => (
              <option key={w.key} value={w.minutes} className="bg-slate-800">Added ≤ {w.label}</option>
            ))}
          </select>
          {atsList.length > 1 && (
            <select
              value={atsFilter}
              onChange={(e) => setAtsFilter(e.target.value)}
              className={`text-xs rounded-lg border px-2 py-1.5 focus:outline-none cursor-pointer ${
                atsFilter ? "bg-sky-600/25 border-sky-500/50 text-sky-300" : "bg-slate-800/60 border-slate-700 text-slate-400"
              }`}
            >
              <option value="" className="bg-slate-800">All ATS</option>
              {atsList.map((a) => <option key={a} value={a} className="bg-slate-800">{a}</option>)}
            </select>
          )}

          <div className="ml-auto flex items-center gap-2 text-xs text-slate-500 whitespace-nowrap">
            <span className="hidden xl:inline text-slate-600" title="Keyboard: j/k move · o open · a applied · s skip">
              ⌨ j/k · o · a · s
            </span>
            <span>
              <strong className="text-slate-300">{filtered.length}</strong>
              {filtersActive && <> of {all.length}</>} {filtered.length === 1 ? "role" : "roles"}
            </span>
            {filtersActive && (
              <button
                onClick={() => { setSearch(""); setFreshOnly(false); setPreparedOnly(false); setHideStale(false); setAddedWithinMin(0); setAtsFilter(""); }}
                className="text-slate-500 hover:text-slate-300 underline"
              >
                clear
              </button>
            )}
          </div>
        </div>
      </div>

      {/* ── Awaiting approval ─────────────────────────────────────────── */}
      <div className="space-y-2 mb-8">
        {groups.map((g) => {
          const openableCount = openableOf(g.items).length;
          const unprepCount = unpreparedOf(g.items);
          return (
          <div key={g.label || "__all"} className={groupBy !== "none" ? "mb-3" : ""}>
            {groupBy !== "none" && (
              <div className="flex items-center gap-2 px-2 py-2 sticky top-[60px] z-10 bg-slate-900/70 backdrop-blur rounded-md">
                <button
                  onClick={() => toggleGroup(g.label)}
                  className="flex items-center gap-2 flex-1 min-w-0 text-left hover:opacity-80 transition-opacity"
                >
                  <ChevronRight className={`w-4 h-4 text-slate-500 transition-transform shrink-0 ${expanded.has(g.label) ? "rotate-90" : ""}`} />
                  {groupBy === "company" && <Building2 className="w-3.5 h-3.5 text-slate-500 shrink-0" />}
                  <span className="text-sm font-semibold text-slate-200 truncate">{g.label || "All"}</span>
                  <span className="text-xs text-slate-500 shrink-0">· {g.items.length}</span>
                  {g.avgScore != null && <span className="text-xs text-slate-600 shrink-0">· avg {g.avgScore}</span>}
                </button>
                {openableCount > 1 && (
                  <button
                    onClick={() => openAll(g.items)}
                    title="Open these application pages in tabs, then click the extension's “Fill ALL”"
                    className="flex items-center gap-1.5 bg-blue-600/20 hover:bg-blue-600/40 border border-blue-500/40 text-blue-300 text-xs font-medium px-2.5 py-1 rounded-lg transition-colors shrink-0"
                  >
                    <ExternalLink className="w-3.5 h-3.5" /> Open all ({Math.min(openableCount, OPEN_CAP)})
                  </button>
                )}
                {unprepCount > 0 && (
                  <button
                    onClick={() => prepareAll(g.items)}
                    title="Tailor résumés for the un-prepared roles in this group (Opus, ~$0.11 each)"
                    className="flex items-center gap-1.5 bg-purple-600/20 hover:bg-purple-600/40 border border-purple-500/40 text-purple-300 text-xs font-medium px-2.5 py-1 rounded-lg transition-colors shrink-0"
                  >
                    <Sparkles className="w-3.5 h-3.5" /> Prepare all ({Math.min(unprepCount, PREP_CAP)})
                  </button>
                )}
                <button
                  onClick={() => appliedAll(g.items, g.label)}
                  title="Mark every role in this group as Applied — only after you've actually submitted them"
                  className="flex items-center gap-1.5 bg-green-600/20 hover:bg-green-600/40 border border-green-500/40 text-green-400 text-xs font-medium px-2.5 py-1 rounded-lg transition-colors shrink-0"
                >
                  <Check className="w-3.5 h-3.5" /> Applied all ({g.items.length})
                </button>
                {groupBy === "company" && g.items[0] && (
                  <button
                    onClick={() => setReferralApp(g.items[0])}
                    title={`Find connections + referral notes at ${g.label}`}
                    className="flex items-center gap-1.5 bg-amber-600/15 hover:bg-amber-600/30 border border-amber-500/40 text-amber-300 text-xs font-medium px-2.5 py-1 rounded-lg transition-colors shrink-0"
                  >
                    <Users className="w-3.5 h-3.5" /> Referral
                  </button>
                )}
              </div>
            )}
            {(groupBy === "none" || expanded.has(g.label)) && (
              <div className="space-y-2 mt-1">
                {g.items.map((app) => <PendingRow key={app.id} app={app} h={rowHandlers} focused={focusedId === app.id} />)}
              </div>
            )}
          </div>
          );
        })}

        {pendingLoading && !pending && (
          <div className="space-y-2">
            {[0, 1, 2].map((i) => (
              <div key={i} className="h-16 bg-slate-800/40 border border-slate-800 rounded-xl animate-pulse" />
            ))}
          </div>
        )}
        {!pendingLoading && !all.length && (
          <div className="text-center py-12 text-slate-600 bg-slate-800/30 border border-slate-800 rounded-xl">
            <div className="text-3xl mb-2">✅</div>
            <p className="text-sm">Nothing waiting for approval.</p>
          </div>
        )}
        {!pendingLoading && !!all.length && !filtered.length && (
          <div className="text-center py-10 text-slate-600 bg-slate-800/30 border border-slate-800 rounded-xl">
            <p className="text-sm">No roles match these filters.</p>
          </div>
        )}
      </div>

      {/* ── Sent, awaiting email confirmation ─────────────────────────── */}
      {!!sent?.length && (
        <>
          <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">
            Sent — awaiting email confirmation ({sent.length})
          </h2>
          <div className="space-y-2 mb-8">
            {sent.map((app) => (
              <ReadonlyRow key={app.id} app={app} onProof={() => setSelectedId(app.id)} onReferral={() => setReferralApp(app)} onAnswers={() => setAnswersApp(app)} tone="teal" />
            ))}
          </div>
        </>
      )}

      {/* ── Follow up / get a referral (highest-leverage post-apply move) ── */}
      {!!followUps.length && (
        <>
          <h2 className="text-xs font-semibold text-amber-300/90 uppercase tracking-wider mb-1 flex items-center gap-2">
            <Users className="w-3.5 h-3.5" /> Follow up — applied 3+ days ago ({followUps.length})
          </h2>
          <p className="text-xs text-slate-500 mb-3">
            A short follow-up — or far better, a <strong className="text-amber-300">referral</strong> — is the single biggest
            callback lever after applying (a warm intro can lift response 5–10×). Hit <strong className="text-amber-300">Referral</strong> for
            ready-to-send notes + the right people to reach.
          </p>
          <div className="space-y-2 mb-8">
            {followUps.map((app) => (
              <ReadonlyRow key={app.id} app={app} onProof={() => setSelectedId(app.id)} onReferral={() => setReferralApp(app)} onAnswers={() => setAnswersApp(app)} tone="teal" highlightReferral />
            ))}
          </div>
        </>
      )}

      {/* ── Submitted (confirmed) ─────────────────────────────────────── */}
      <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">
        Submitted {submitted?.length ? `(${submitted.length})` : ""}
      </h2>
      <div className="space-y-2">
        {submittedRest.map((app) => (
          <ReadonlyRow key={app.id} app={app} onProof={() => setSelectedId(app.id)} onReferral={() => setReferralApp(app)} onAnswers={() => setAnswersApp(app)} tone="green" />
        ))}
        {!submitted?.length && (
          <div className="text-center py-10 text-slate-600">
            <p className="text-sm">No confirmed submissions yet.</p>
          </div>
        )}
      </div>

      {selectedId && (
        <ProofModal
          appId={selectedId}
          onClose={() => setSelectedId(null)}
          onDecide={(action, reason) => { decide.mutate({ id: selectedId, action, reason }); setSelectedId(null); }}
        />
      )}
      {referralApp && (
        <ReferralModal appId={referralApp.id} company={referralApp.company_name || ""} role={referralApp.role_title || ""} onClose={() => setReferralApp(null)} />
      )}
      {answersApp && (
        <FormAnswersModal appId={answersApp.id} company={answersApp.company_name || ""} role={answersApp.role_title || ""} onClose={() => setAnswersApp(null)} />
      )}
    </div>
  );
}

interface RowHandlers {
  tailor: { mutate: (id: string) => void };
  tailoringId: string | null | undefined;
  openApply: (app: Application) => void;
  onApplied: (id: string) => void;
  onSkip: (id: string) => void;
  busyId: string | null | undefined;
  setReferralApp: (a: Application) => void;
  setAnswersApp: (a: Application) => void;
  setSelectedId: (id: string) => void;
}

function PendingRow({ app, h, focused }: { app: Application; h: RowHandlers; focused?: boolean }) {
  return (
    <div
      id={`app-${app.id}`}
      className={`bg-slate-800/50 border rounded-xl px-5 py-4 flex items-center justify-between transition-colors ${
        focused ? "border-blue-500/70 ring-1 ring-blue-500/40 bg-slate-700/30" : "border-slate-700 hover:bg-slate-700/30"
      }`}
    >
      <div className="flex items-center gap-4 min-w-0">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-slate-100 truncate">{app.company_name}</div>
          <div className="text-xs text-slate-400 truncate mt-0.5">{app.role_title}</div>
          {app.location && (
            <div className="text-[11px] text-slate-500 truncate mt-0.5">
              📍 {app.location}
              {app.multi_location && (
                <span className="ml-1 text-amber-400" title="Offered in the US and abroad — apply to the US office">
                  · multi-location, apply to US office
                </span>
              )}
            </div>
          )}
        </div>
        <ScoreBadge score={app.quality_score} fit={app.fit_score} />
        <PostedBadge postedAt={app.posted_at} />
        {addedAgo(app.created_at) && (
          <span className="text-[11px] px-1.5 py-0.5 rounded border border-slate-600/40 bg-slate-700/20 text-slate-500 hidden md:inline-block" title={`Entered your queue ${addedAgo(app.created_at)}`}>
            added {addedAgo(app.created_at)}
          </span>
        )}
        <AtsBadge url={app.ats_url} />
      </div>
      <div className="flex items-center gap-2 shrink-0 ml-4">
        {app.quality_score == null && (
          <button
            onClick={() => h.tailor.mutate(app.id)}
            disabled={h.tailoringId === app.id}
            title="Generate the tailored Opus résumé for this role"
            className="flex items-center gap-1.5 bg-purple-600/20 hover:bg-purple-600/40 border border-purple-500/40 text-purple-300 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors disabled:opacity-50"
          >
            <Sparkles className="w-3.5 h-3.5" /> {h.tailoringId === app.id ? "Tailoring…" : "Prepare résumé"}
          </button>
        )}
        <button
          onClick={() => h.openApply(app)}
          disabled={!app.ats_url}
          title={app.ats_url ?? "no application URL"}
          className="flex items-center gap-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold px-3 py-1.5 rounded-lg transition-colors disabled:opacity-40"
        >
          Open &amp; Apply <ExternalLink className="w-3.5 h-3.5" />
        </button>
        <button
          onClick={() => h.setReferralApp(app)}
          title="Find connections + get ready-to-send referral notes"
          className="flex items-center gap-1.5 bg-amber-600/15 hover:bg-amber-600/30 border border-amber-500/40 text-amber-300 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors"
        >
          <Users className="w-3.5 h-3.5" /> Referral
        </button>
        <button
          onClick={() => h.setAnswersApp(app)}
          title="View the saved LLM answers drafted for this role's form questions"
          className="flex items-center gap-1.5 bg-sky-600/15 hover:bg-sky-600/30 border border-sky-500/40 text-sky-300 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors"
        >
          <MessageSquareText className="w-3.5 h-3.5" /> Answers
        </button>
        <button
          onClick={() => h.setSelectedId(app.id)}
          className="flex items-center gap-1.5 bg-slate-700/40 hover:bg-slate-700 border border-slate-600 text-slate-300 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors"
        >
          <Eye className="w-3.5 h-3.5" /> Proof
        </button>
        <button
          disabled={h.busyId === app.id}
          onClick={() => h.onApplied(app.id)}
          className="flex items-center gap-1.5 bg-green-600/20 hover:bg-green-600/40 border border-green-500/40 text-green-400 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors disabled:opacity-50"
        >
          <Check className="w-3.5 h-3.5" /> {h.busyId === app.id ? "…" : "Applied"}
        </button>
        <button
          disabled={h.busyId === app.id}
          onClick={() => h.onSkip(app.id)}
          className="flex items-center gap-1.5 bg-red-600/10 hover:bg-red-600/30 border border-red-500/40 text-red-400 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors disabled:opacity-50"
        >
          <X className="w-3.5 h-3.5" /> Skip
        </button>
      </div>
    </div>
  );
}

function ReadonlyRow({ app, onProof, onReferral, onAnswers, tone, highlightReferral }: { app: Application; onProof: () => void; onReferral?: () => void; onAnswers?: () => void; tone: "teal" | "green"; highlightReferral?: boolean }) {
  const dot = tone === "teal" ? "bg-teal-400" : "bg-green-400";
  return (
    <div className="bg-slate-800/50 border border-slate-700 rounded-xl px-5 py-4 flex items-center justify-between hover:bg-slate-700/30 transition-colors">
      <div className="flex items-center gap-4 min-w-0">
        <span className={`w-2 h-2 rounded-full ${dot} shrink-0`} />
        <div className="min-w-0">
          <div className="text-sm font-semibold text-slate-100 truncate">{app.company_name}</div>
          <div className="text-xs text-slate-400 truncate mt-0.5">{app.role_title}</div>
        </div>
        <ScoreBadge score={app.quality_score} fit={app.fit_score} />
        <span className="text-xs text-slate-600 hidden md:block">
          {STATUS_LABEL[app.status] ?? app.status} · {fmtRelative(app.submitted_at ?? app.updated_at)}
        </span>
      </div>
      <div className="flex items-center gap-2 shrink-0 ml-4">
        {onAnswers && (
          <button onClick={onAnswers} title="View the saved LLM answers drafted for this role's form questions"
            className="flex items-center gap-1.5 bg-sky-600/15 hover:bg-sky-600/30 border border-sky-500/40 text-sky-300 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors">
            <MessageSquareText className="w-3.5 h-3.5" /> Answers
          </button>
        )}
        {onReferral && (
          <button onClick={onReferral} title="Get ready-to-send referral notes + find connections — the biggest callback lever after applying"
            className={`flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg transition-colors ${
              highlightReferral
                ? "bg-amber-500 hover:bg-amber-400 text-slate-900 border border-amber-400 font-semibold"
                : "bg-amber-600/15 hover:bg-amber-600/30 border border-amber-500/40 text-amber-300 font-medium"
            }`}>
            <Users className="w-3.5 h-3.5" /> Referral
          </button>
        )}
        <button onClick={onProof}
          className="flex items-center gap-2 bg-blue-600/20 hover:bg-blue-600/40 border border-blue-500/40 text-blue-400 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors">
          <Eye className="w-3.5 h-3.5" /> View Proof
        </button>
      </div>
    </div>
  );
}
