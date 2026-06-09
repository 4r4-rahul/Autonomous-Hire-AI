"use client";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, type Job } from "@/lib/api";
import { fmtRelative, STATUS_COLOR, STATUS_LABEL } from "@/lib/utils";
import { ExternalLink, RefreshCw } from "lucide-react";
import { STEPS } from "@/lib/steps";


function ScoreBadge({ score }: { score: number | null }) {
  if (score == null) return <span className="text-slate-600">—</span>;
  const color = score >= 80 ? "text-green-400" : score >= 65 ? "text-yellow-400" : "text-red-400";
  return <span className={`font-semibold ${color}`}>{score}</span>;
}

function StatusBadge({ status }: { status: string }) {
  const cls = STATUS_COLOR[status] ?? "bg-slate-500/20 text-slate-400 border-slate-500/40";
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full border ${cls}`}>
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

export default function LinkedInPage() {
  const { data: status, refetch: refetchStatus } = useQuery({
    queryKey: ["pipeline-status"],
    queryFn: api.pipelineStatus,
    refetchInterval: 3_000,
  });
  const { data: queue } = useQuery({
    queryKey: ["queue"],
    queryFn: () => api.getQueue(60),
    refetchInterval: 15_000,
  });
  const { data: submitted } = useQuery({
    queryKey: ["apps-submitted"],
    queryFn: () => api.listApps("status=submitted&limit=50"),
    refetchInterval: 15_000,
  });
  const { data: blocked } = useQuery({
    queryKey: ["apps-blocked"],
    queryFn: () => api.listApps("status=blocked&limit=50"),
    refetchInterval: 15_000,
  });
  const { data: pending } = useQuery({
    queryKey: ["apps-pending"],
    queryFn: () => api.listApps("status=pending_approval&limit=50"),
    refetchInterval: 10_000,
  });
  const { data: manual } = useQuery({
    queryKey: ["apps-manual"],
    queryFn: () => api.listApps("status=pending_manual_submit&limit=50"),
    refetchInterval: 10_000,
  });

  const [logs, setLogs] = useState<string[]>([]);
  const logRef = useRef<HTMLDivElement>(null);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!status?.running) return;
    const es = new EventSource("http://localhost:8000/api/v1/pipeline/stream");
    esRef.current = es;
    es.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === "text") setLogs((l) => [...l.slice(-199), msg.line]);
        if (msg.type === "log" && msg.entry?.message) setLogs((l) => [...l.slice(-199), msg.entry.message]);
        if (msg.type === "done") { es.close(); refetchStatus(); }
      } catch {}
    };
    return () => es.close();
  }, [status?.running]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [logs]);

  const blockerGroups: Record<string, typeof blocked> = {};
  (blocked ?? []).forEach((app) => {
    const key = app.rejection_reason?.split("\n")[0]?.slice(0, 80) ?? "Unknown";
    if (!blockerGroups[key]) blockerGroups[key] = [];
    blockerGroups[key]!.push(app);
  });

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-100">LinkedIn Pipeline</h1>
          <p className="text-sm text-slate-500 mt-0.5">
            Last run: {fmtRelative(status?.last_run_at)} · Queue: {status?.queue_depth ?? 0} jobs
          </p>
        </div>
        {status?.running && (
          <div className="flex items-center gap-2 bg-green-500/10 border border-green-500/30 rounded-lg px-4 py-2">
            <span className="w-2 h-2 rounded-full bg-green-400 animate-pulse" />
            <span className="text-sm text-green-400 font-medium">{status.current_step ?? "Running…"}</span>
          </div>
        )}
      </div>

      {/* Pipeline visualizer */}
      <div className="bg-slate-800/40 border border-slate-700 rounded-xl p-5">
        <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-4">Pipeline Steps</h2>
        <div className="flex flex-wrap gap-2 items-center">
          {STEPS.map((step, i) => {
            const active = status?.running &&
              status.current_step?.toLowerCase().includes(step.name.split(" ")[0].toLowerCase());
            return (
              <div key={step.n} className="flex items-center gap-2">
                <div className={`rounded-lg px-3 py-2 border text-center min-w-[105px] transition-all ${
                  active ? "bg-blue-600/20 border-blue-500 shadow-blue-500/20 shadow-lg" : "bg-slate-900/50 border-slate-700"
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

      {/* Live log */}
      {status?.running && (
        <div className="bg-slate-900 border border-slate-700 rounded-xl p-4">
          <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">Live Log</h2>
          <div ref={logRef} className="h-40 overflow-y-auto font-mono text-xs text-slate-400 space-y-0.5">
            {logs.length === 0
              ? <p className="text-slate-600">Waiting for log output…</p>
              : logs.map((l, i) => <div key={i}>{l}</div>)
            }
          </div>
        </div>
      )}

      {/* Stats row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {[
          { label: "In Queue",      value: status?.queue_depth ?? 0,    color: "border-blue-500"   },
          { label: "Submitted",     value: submitted?.length ?? 0,      color: "border-green-500"  },
          { label: "Pending Review",value: pending?.length ?? 0,        color: "border-yellow-500" },
          { label: "Blocked",       value: blocked?.length ?? 0,        color: "border-red-500"    },
        ].map(({ label, value, color }) => (
          <div key={label} className={`bg-slate-800/50 border border-slate-700 rounded-xl p-4 border-t-2 ${color}`}>
            <div className="text-2xl font-bold text-slate-100">{value}</div>
            <div className="text-xs text-slate-400 uppercase tracking-wider mt-1">{label}</div>
          </div>
        ))}
      </div>

      {/* Queue */}
      <div className="bg-slate-800/40 border border-slate-700 rounded-xl">
        <div className="px-5 py-4 border-b border-slate-700 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-300">Up Next — Queue ({queue?.length ?? 0})</h2>
          <span className="text-xs text-slate-500">Sorted by fit score</span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-slate-500 uppercase tracking-wider border-b border-slate-700">
                <th className="px-4 py-2 text-left">Company</th>
                <th className="px-4 py-2 text-left">Role</th>
                <th className="px-4 py-2 text-left">Location</th>
                <th className="px-4 py-2 text-center">Score</th>
                <th className="px-4 py-2 text-left">ATS</th>
                <th className="px-4 py-2 text-left">Found</th>
              </tr>
            </thead>
            <tbody>
              {(queue ?? []).slice(0, 30).map((job) => (
                <tr key={job.id} className="border-b border-slate-800 hover:bg-slate-700/20 transition-colors">
                  <td className="px-4 py-2 text-slate-200 font-medium">{job.company_name}</td>
                  <td className="px-4 py-2 text-slate-300 max-w-[260px] truncate">{job.title}</td>
                  <td className="px-4 py-2 text-slate-400 text-xs">{job.location ?? "—"}</td>
                  <td className="px-4 py-2 text-center"><ScoreBadge score={job.fit_score} /></td>
                  <td className="px-4 py-2 text-xs text-slate-500">{job.ats_type ?? "—"}</td>
                  <td className="px-4 py-2 text-xs text-slate-500">{fmtRelative(job.discovered_at)}</td>
                </tr>
              ))}
              {!queue?.length && (
                <tr><td colSpan={6} className="px-4 py-6 text-center text-slate-600">Queue is empty</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Submitted */}
      <div className="bg-slate-800/40 border border-slate-700 rounded-xl">
        <div className="px-5 py-4 border-b border-slate-700">
          <h2 className="text-sm font-semibold text-slate-300">Submitted ({submitted?.length ?? 0})</h2>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-slate-500 uppercase tracking-wider border-b border-slate-700">
                <th className="px-4 py-2 text-left">Company</th>
                <th className="px-4 py-2 text-left">Role</th>
                <th className="px-4 py-2 text-center">Quality</th>
                <th className="px-4 py-2 text-left">ATS URL</th>
                <th className="px-4 py-2 text-left">Submitted</th>
              </tr>
            </thead>
            <tbody>
              {(submitted ?? []).map((app) => (
                <tr key={app.id} className="border-b border-slate-800 hover:bg-slate-700/20 transition-colors">
                  <td className="px-4 py-2 text-slate-200 font-medium">{app.company_name ?? "—"}</td>
                  <td className="px-4 py-2 text-slate-300 max-w-[260px] truncate">{app.role_title ?? "—"}</td>
                  <td className="px-4 py-2 text-center"><ScoreBadge score={app.quality_score} /></td>
                  <td className="px-4 py-2">
                    {app.ats_url ? (
                      <a href={app.ats_url} target="_blank" rel="noreferrer"
                        className="flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300">
                        <ExternalLink className="w-3 h-3" />
                        <span className="max-w-[200px] truncate">{(() => { try { return new URL(app.ats_url).hostname; } catch { return app.ats_url; } })()}</span>
                      </a>
                    ) : "—"}
                  </td>
                  <td className="px-4 py-2 text-xs text-slate-500">{fmtRelative(app.submitted_at)}</td>
                </tr>
              ))}
              {!submitted?.length && (
                <tr><td colSpan={5} className="px-4 py-6 text-center text-slate-600">No submissions yet</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Pending approval */}
      {(pending?.length ?? 0) > 0 && (
        <div className="bg-yellow-500/5 border border-yellow-500/30 rounded-xl">
          <div className="px-5 py-4 border-b border-yellow-500/20">
            <h2 className="text-sm font-semibold text-yellow-400">Pending Your Review ({pending!.length})</h2>
            <p className="text-xs text-slate-500 mt-0.5">Go to the Approvals tab to review resumes and approve/reject.</p>
          </div>
          <div className="divide-y divide-slate-800">
            {(pending ?? []).map((app) => (
              <div key={app.id} className="px-5 py-3 flex items-center justify-between">
                <div>
                  <span className="text-sm font-medium text-slate-200">{app.company_name}</span>
                  <span className="text-xs text-slate-500 ml-2">{app.role_title}</span>
                </div>
                <ScoreBadge score={app.quality_score} />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Manual submit */}
      {(manual?.length ?? 0) > 0 && (
        <div className="bg-orange-500/5 border border-orange-500/30 rounded-xl">
          <div className="px-5 py-4 border-b border-orange-500/20">
            <h2 className="text-sm font-semibold text-orange-400">Manual Submit Required ({manual!.length})</h2>
            <p className="text-xs text-slate-500 mt-0.5">ATS automation failed — open the link and submit manually.</p>
          </div>
          <div className="divide-y divide-slate-800">
            {(manual ?? []).map((app) => (
              <div key={app.id} className="px-5 py-3 flex items-center justify-between">
                <div>
                  <span className="text-sm font-medium text-slate-200">{app.company_name}</span>
                  <span className="text-xs text-slate-500 ml-2">{app.role_title}</span>
                </div>
                {app.ats_url && (
                  <a href={app.ats_url} target="_blank" rel="noreferrer"
                    className="flex items-center gap-1 text-xs text-orange-400 hover:text-orange-300 border border-orange-500/40 rounded px-2 py-1">
                    <ExternalLink className="w-3 h-3" /> Open ATS
                  </a>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Blockers */}
      <div className="bg-slate-800/40 border border-slate-700 rounded-xl">
        <div className="px-5 py-4 border-b border-slate-700">
          <h2 className="text-sm font-semibold text-slate-300">Blocked ({blocked?.length ?? 0})</h2>
        </div>
        {Object.keys(blockerGroups).length === 0 ? (
          <p className="px-5 py-6 text-sm text-slate-600">No blocked applications.</p>
        ) : (
          <div className="divide-y divide-slate-800">
            {Object.entries(blockerGroups).map(([reason, apps]) => (
              <div key={reason} className="px-5 py-4">
                <div className="flex items-center gap-2 mb-2">
                  <span className="text-xs font-semibold text-red-400 bg-red-500/10 border border-red-500/30 rounded px-2 py-0.5">
                    {apps!.length}×
                  </span>
                  <span className="text-sm text-slate-400">{reason}</span>
                </div>
                <div className="flex flex-wrap gap-2">
                  {apps!.map((app) => (
                    <span key={app.id} className="text-xs bg-slate-800 border border-slate-700 rounded px-2 py-1 text-slate-300">
                      {app.company_name}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Rate limits */}
      <div className="bg-slate-800/40 border border-slate-700 rounded-xl p-5">
        <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">Rate Limits</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
          {[
            ["LinkedIn Search",    "10 / hr · 60 / day"],
            ["LinkedIn Job View",  "40 / hr · 200 / day"],
            ["Batch size",         "40 jobs / run"],
            ["Apply cap",          "1 submission / run"],
          ].map(([label, val]) => (
            <div key={label as string} className="bg-slate-900/50 rounded-lg p-3">
              <div className="text-xs text-slate-500 mb-1">{label}</div>
              <div className="font-mono text-xs text-slate-300">{val}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
