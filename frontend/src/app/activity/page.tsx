"use client";
import { useQuery } from "@tanstack/react-query";
import { api, type PipelineRun } from "@/lib/api";
import { fmtDate, fmtRelative } from "@/lib/utils";
import { CheckCircle, XCircle, Clock, AlertCircle, Zap } from "lucide-react";

function RunStatusIcon({ status }: { status: string }) {
  if (status === "completed") return <CheckCircle className="w-4 h-4 text-green-400" />;
  if (status === "failed")    return <XCircle className="w-4 h-4 text-red-400" />;
  if (status === "running")   return <Zap className="w-4 h-4 text-blue-400 animate-pulse" />;
  return <Clock className="w-4 h-4 text-slate-500" />;
}

function duration(start: string, end: string | null): string {
  if (!end) return "running…";
  const s = Math.round((new Date(end).getTime() - new Date(start).getTime()) / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

export default function ActivityPage() {
  const { data: runs, isLoading } = useQuery({
    queryKey: ["pipeline-runs"],
    queryFn: api.pipelineRuns,
    refetchInterval: 15_000,
  });

  const { data: recentApps } = useQuery({
    queryKey: ["apps-recent"],
    queryFn: () => api.listApps("sort_by=updated_at&limit=20"),
    refetchInterval: 15_000,
  });

  const { data: status } = useQuery({
    queryKey: ["pipeline-status"],
    queryFn: api.pipelineStatus,
    refetchInterval: 5_000,
  });

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-slate-100">Activity</h1>
        <p className="text-sm text-slate-500 mt-0.5">Pipeline run history and recent events</p>
      </div>

      {/* Current run status */}
      {status?.running && (
        <div className="bg-blue-500/10 border border-blue-500/30 rounded-xl p-5">
          <div className="flex items-center gap-3 mb-3">
            <span className="w-2 h-2 rounded-full bg-blue-400 animate-pulse" />
            <span className="text-sm font-semibold text-blue-400">Pipeline Running</span>
            <span className="text-xs text-slate-500">{status.current_step ?? "…"}</span>
          </div>
          <div className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <span className="text-slate-500">Queue depth</span>
              <span className="ml-2 font-semibold text-slate-200">{status.queue_depth}</span>
            </div>
            <div>
              <span className="text-slate-500">Submitted today</span>
              <span className="ml-2 font-semibold text-slate-200">{status.submitted_today}</span>
            </div>
          </div>
        </div>
      )}

      {/* Pipeline runs */}
      <div>
        <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Pipeline Runs</h2>
        <div className="bg-slate-800/40 border border-slate-700 rounded-xl overflow-hidden">
          {isLoading ? (
            <div className="px-5 py-8 text-center text-slate-600 text-sm">Loading…</div>
          ) : !runs?.length ? (
            <div className="px-5 py-8 text-center text-slate-600 text-sm">No pipeline runs recorded yet.</div>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-slate-500 uppercase tracking-wider border-b border-slate-700 bg-slate-900/40">
                  <th className="px-4 py-3 text-left">Status</th>
                  <th className="px-4 py-3 text-left">Lane</th>
                  <th className="px-4 py-3 text-left">Job</th>
                  <th className="px-4 py-3 text-left">Started</th>
                  <th className="px-4 py-3 text-center">Duration</th>
                  <th className="px-4 py-3 text-center">New</th>
                  <th className="px-4 py-3 text-center">Tailored/Applied</th>
                  <th className="px-4 py-3 text-left">Error</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.id} className="border-b border-slate-800/80 hover:bg-slate-700/20 transition-colors">
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <RunStatusIcon status={run.status} />
                        <span className={`text-xs font-medium ${
                          run.status === "completed" ? "text-green-400"
                          : run.status === "failed" ? "text-red-400"
                          : run.status === "running" ? "text-blue-400"
                          : "text-slate-400"
                        }`}>{run.status}</span>
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      {(() => {
                        const lane = run.trigger.includes(":") ? run.trigger.split(":")[0] : run.trigger;
                        const tone = lane === "contract" ? "bg-amber-600/20 text-amber-300"
                                   : lane === "fulltime" ? "bg-sky-600/20 text-sky-300"
                                   : "bg-slate-700 text-slate-300";
                        return <span className={`text-xs rounded px-2 py-0.5 font-mono ${tone}`}>{lane}</span>;
                      })()}
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-400 font-mono">
                      {run.trigger.includes(":") ? run.trigger.split(":").slice(1).join(":") : "—"}
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-400">{fmtDate(run.started_at)}</td>
                    <td className="px-4 py-3 text-center text-xs text-slate-400 font-mono">
                      {duration(run.started_at, run.finished_at)}
                    </td>
                    <td className="px-4 py-3 text-center text-slate-300 font-semibold">
                      {run.scan_new_jobs || "—"}
                    </td>
                    <td className="px-4 py-3 text-center font-semibold">
                      <span className={run.jobs_submitted ? "text-green-400" : "text-slate-500"}>
                        {run.jobs_submitted || "—"}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-xs text-red-400 max-w-[200px] truncate">
                      {run.error ?? ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {/* Recent application events */}
      <div>
        <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Recent Application Activity</h2>
        <div className="space-y-2">
          {(recentApps ?? []).map((app) => (
            <div key={app.id} className="bg-slate-800/40 border border-slate-700 rounded-lg px-4 py-3 flex items-center justify-between">
              <div className="flex items-center gap-3">
                {app.status === "submitted"        && <CheckCircle className="w-4 h-4 text-green-400 shrink-0" />}
                {app.status === "blocked"          && <XCircle className="w-4 h-4 text-red-400 shrink-0" />}
                {app.status === "pending_approval" && <AlertCircle className="w-4 h-4 text-yellow-400 shrink-0" />}
                {!["submitted", "blocked", "pending_approval"].includes(app.status) && (
                  <Clock className="w-4 h-4 text-slate-500 shrink-0" />
                )}
                <div>
                  <span className="text-sm text-slate-200 font-medium">{app.company_name ?? "Unknown"}</span>
                  <span className="text-xs text-slate-500 ml-2">{app.role_title}</span>
                </div>
              </div>
              <div className="flex items-center gap-3">
                <span className={`text-xs px-2 py-0.5 rounded-full border ${
                  app.status === "submitted" ? "bg-green-500/20 text-green-400 border-green-500/40"
                  : app.status === "blocked" ? "bg-red-500/20 text-red-400 border-red-500/40"
                  : app.status === "pending_approval" ? "bg-yellow-500/20 text-yellow-400 border-yellow-500/40"
                  : "bg-slate-500/20 text-slate-400 border-slate-500/40"
                }`}>{app.status.replace(/_/g, " ")}</span>
                <span className="text-xs text-slate-600 w-20 text-right">{fmtRelative(app.updated_at)}</span>
              </div>
            </div>
          ))}
          {!recentApps?.length && (
            <div className="text-center text-slate-600 py-8 text-sm">No application activity yet.</div>
          )}
        </div>
      </div>
    </div>
  );
}
