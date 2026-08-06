"use client";
import { useQuery } from "@tanstack/react-query";
import { api, BASE } from "@/lib/api";
import { fmtRelative } from "@/lib/utils";
import { STEPS } from "@/lib/steps";

// Standalone Live Meeting Assistant (coach window). Deep-linked with ?company= so it auto-loads
// the prep brief from /insights/prep. Opened as a small window (coach sits beside your call).
const LMA_URL = process.env.NEXT_PUBLIC_LMA_URL ?? "http://localhost:3100";
function openCoach(company: string, role: string, round?: string) {
  const qs = new URLSearchParams({ company, role, mode: "coach", ...(round ? { round } : {}) }).toString();
  // Interview-sized, resizable window (drag it to a second monitor as a teleprompter). Tall enough for
  // system-design deep-dives + diagrams without constant scrolling.
  window.open(
    `${LMA_URL}/?${qs}`,
    "lma_coach",
    "width=720,height=1040,resizable=yes,scrollbars=yes",
  );
}

function KPI({ label, value, sub, color }: { label: string; value: number | string; sub?: string; color: string }) {
  return (
    <div className={`bg-slate-800/50 border border-slate-700 rounded-xl p-5 border-t-2 ${color}`}>
      <div className="text-3xl font-bold text-slate-100">{value}</div>
      <div className="text-xs text-slate-400 uppercase tracking-wider mt-1">{label}</div>
      {sub && <div className="text-xs text-slate-500 mt-1">{sub}</div>}
    </div>
  );
}


export default function DashboardPage() {
  const { data: stats }  = useQuery({ queryKey: ["stats"],           queryFn: api.stats,          refetchInterval: 10_000 });
  const { data: status } = useQuery({ queryKey: ["pipeline-status"], queryFn: api.pipelineStatus, refetchInterval: 3_000  });
  const { data: insights } = useQuery({ queryKey: ["insights"],      queryFn: api.insights,       refetchInterval: 30_000 });
  const t = insights?.totals;
  const interviews = insights?.interviews ?? [];

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-slate-100">Dashboard</h1>
          <p className="text-sm text-slate-500 mt-0.5">
            Last run: {fmtRelative(status?.last_run_at)} · {status?.submitted_today ?? 0} submitted today
          </p>
        </div>
        {status?.running && (
          <div className="flex items-center gap-2 bg-green-500/10 border border-green-500/30 rounded-lg px-4 py-2">
            <span className="w-2 h-2 rounded-full bg-green-400 animate-pulse" />
            <span className="text-sm text-green-400 font-medium">{status.current_step ?? "Running…"}</span>
          </div>
        )}
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-4 mb-8">
        <KPI label="Jobs Found"     value={stats?.total_jobs ?? "—"}      color="border-blue-500"   />
        <KPI label="LI Queue"       value={stats?.queued_jobs ?? "—"}     color="border-indigo-500" sub={`of ${stats?.linkedin_jobs ?? 0} LinkedIn`} />
        <KPI label="Submitted"      value={stats?.submitted ?? "—"}       color="border-green-500"  />
        <KPI label="Pending Review" value={stats?.pending_review ?? "—"}  color="border-yellow-500" />
        <KPI label="Manual Submit"  value={stats?.pending_manual ?? "—"}  color="border-orange-500" />
        <KPI label="Blocked"        value={stats?.blocked ?? "—"}         color="border-red-500"    />
      </div>

      {/* Outcomes — the results that matter (first-party only, staffing/self filtered out) */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <KPI label="Interviews / Offers" value={t?.interviews_or_offers ?? "—"} color="border-emerald-500" sub="real employer responses" />
        <KPI label="Responses"           value={t?.responded ?? "—"}           color="border-teal-500"    sub="first-party + ATS" />
        <KPI label="Response Rate"       value={t ? `${(t.response_rate * 100).toFixed(1)}%` : "—"} color="border-cyan-500" />
        <KPI label="Ghosted"             value={t?.ghosted ?? "—"}             color="border-slate-500"   sub="no reply 21d+" />
      </div>

      {/* Interviews & offers — the wins, front and center */}
      <div className="mb-8">
        <h2 className="text-sm font-semibold text-emerald-300 uppercase tracking-wider mb-3 flex items-center gap-2">
          🎯 Interviews &amp; Offers {interviews.length ? `(${interviews.length})` : ""}
        </h2>
        {interviews.length ? (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
            {interviews.map((iv, i) => (
              <div key={`${iv.company}-${i}`} className="bg-emerald-500/10 border border-emerald-500/30 rounded-xl px-4 py-3">
                <div className="flex items-center justify-between gap-2">
                  <div className="text-sm font-semibold text-slate-100 truncate">{iv.company}</div>
                  <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 shrink-0">{iv.status}</span>
                </div>
                <div className="text-xs text-slate-400 truncate mt-0.5">{iv.role}</div>
                <div className="flex items-center justify-between gap-2 mt-1">
                  {iv.at ? <div className="text-[11px] text-slate-500">{fmtRelative(iv.at)}</div> : <span />}
                  {iv.prep_doc_path && (
                    <a
                      href={`${BASE}/insights/prep?company=${encodeURIComponent(iv.company)}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-sky-500/20 text-sky-300 border border-sky-500/40 hover:bg-sky-500/30 shrink-0"
                    >
                      📋 Prep
                    </a>
                  )}
                </div>

                {/* Practice tools — grouped: live Coach (drag to a 2nd monitor) vs Mock interviewer */}
                <div className="mt-2.5 space-y-1.5">
                  <div className="flex items-start gap-2">
                    <span className="w-10 shrink-0 pt-1 text-[9px] font-semibold uppercase tracking-wider text-violet-300/70">Coach</span>
                    <div className="flex flex-wrap gap-1.5">
                      {[
                        { label: "🎤 General",       on: () => openCoach(iv.company, iv.role) },
                        { label: "🎥 Deep Dive",     on: () => openCoach(iv.company, iv.role, "deepdive") },
                        { label: "📐 System Design", on: () => openCoach(iv.company, iv.role, "architecture") },
                        { label: "🐧 Linux",         on: () => openCoach(iv.company, iv.role, "linux") },
                      ].map((b) => (
                        <button
                          key={b.label}
                          onClick={b.on}
                          className="text-[10px] uppercase tracking-wide px-2 py-0.5 rounded bg-violet-500/15 text-violet-300 border border-violet-500/30 hover:bg-violet-500/30"
                        >
                          {b.label}
                        </button>
                      ))}
                    </div>
                  </div>

                  <div className="flex items-start gap-2">
                    <span className="w-10 shrink-0 pt-1 text-[9px] font-semibold uppercase tracking-wider text-amber-300/70">Mock</span>
                    <div className="flex flex-wrap gap-1.5">
                      {[
                        { label: "🎯 Behavioral",    q: "" },
                        { label: "📐 System Design", q: "&mode=system_design" },
                        { label: "🐧 Linux",         q: "&mode=linux" },
                      ].map((b) => (
                        <a
                          key={b.label}
                          href={`/mock?company=${encodeURIComponent(iv.company)}&role=${encodeURIComponent(iv.role)}${b.q}`}
                          className="text-[10px] uppercase tracking-wide px-2 py-0.5 rounded bg-amber-500/15 text-amber-300 border border-amber-500/30 hover:bg-amber-500/30"
                        >
                          {b.label}
                        </a>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-sm text-slate-600 bg-slate-800/30 border border-slate-800 rounded-xl px-4 py-6 text-center">
            No interviews recorded yet — they appear here automatically as real employers respond.
          </div>
        )}
      </div>

      {/* Pipeline step visualizer */}
      <div className="mb-8">
        <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Pipeline Steps</h2>
        <div className="flex flex-wrap gap-2 items-center">
          {STEPS.map((step, i) => {
            const active = status?.running &&
              status.current_step?.toLowerCase().includes(step.name.split(" ")[0].toLowerCase());
            return (
              <div key={step.n} className="flex items-center gap-2">
                <div className={`rounded-lg px-3 py-2 border text-center min-w-[105px] transition-all ${
                  active ? "bg-blue-600/20 border-blue-500 shadow-lg shadow-blue-500/10" : "bg-slate-800/50 border-slate-700"
                }`}>
                  <div className={`text-xs font-bold mb-0.5 ${active ? "text-blue-400" : "text-slate-600"}`}>STEP {step.n}</div>
                  <div className="text-sm font-medium text-slate-200">{step.name}</div>
                  <div className="text-xs text-slate-500 mt-0.5">{step.desc}</div>
                </div>
                {i < STEPS.length - 1 && <span className="text-slate-700 text-lg shrink-0">→</span>}
              </div>
            );
          })}
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-slate-800/50 border border-slate-700 rounded-xl p-5">
          <h3 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Scoring Stats</h3>
          <div className="space-y-2 text-sm">
            {[
              ["Pass rate (threshold 65)", `${stats?.pass_rate ?? 0}%`,         "text-blue-400"],
              ["Avg quality score",         `${stats?.avg_quality_score ?? 0}/100`, "text-purple-400"],
              ["Total applications",         stats?.total_applications ?? 0,      "text-slate-300"],
            ].map(([label, val, color]) => (
              <div key={label as string} className="flex justify-between">
                <span className="text-slate-400">{label}</span>
                <span className={`font-semibold ${color}`}>{val}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="bg-slate-800/50 border border-slate-700 rounded-xl p-5">
          <h3 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Rate Limits</h3>
          <div className="space-y-2 text-sm">
            {[
              ["LinkedIn Search",   "10/hr · 60/day"],
              ["LinkedIn Job View", "40/hr · 200/day"],
              ["Batch size",        "40 jobs per run"],
              ["Apply cap",         "Stop after 1 submission"],
            ].map(([label, val]) => (
              <div key={label} className="flex justify-between">
                <span className="text-slate-400">{label}</span>
                <span className="font-mono text-xs text-slate-300">{val}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
