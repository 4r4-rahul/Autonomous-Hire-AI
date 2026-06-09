"use client";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { fmtDate, STATUS_COLOR, STATUS_LABEL } from "@/lib/utils";
import { ExternalLink, Search, Eye } from "lucide-react";
import { ProofModal } from "@/components/ProofModal";

const STATUS_TABS = [
  { key: "",                    label: "All"       },
  { key: "submitted",           label: "Submitted" },
  { key: "pending_approval",    label: "Pending"   },
  { key: "pending_manual_submit", label: "Manual"  },
  { key: "blocked",             label: "Blocked"   },
  { key: "skipped",             label: "Skipped"   },
];

function ScoreBadge({ score }: { score: number | null }) {
  if (score == null) return <span className="text-slate-600 text-xs">—</span>;
  const bg = score >= 80 ? "bg-green-500/20 text-green-400 border-green-500/40"
           : score >= 65 ? "bg-yellow-500/20 text-yellow-400 border-yellow-500/40"
           : "bg-red-500/20 text-red-400 border-red-500/40";
  return <span className={`text-xs px-2 py-0.5 rounded-full border ${bg} font-semibold`}>{score}</span>;
}

export default function ApplicationsPage() {
  const [statusFilter, setStatusFilter] = useState("");
  const [search, setSearch] = useState("");
  const [proofId, setProofId] = useState<string | null>(null);

  const params = [
    statusFilter && `status=${statusFilter}`,
    search && `company=${encodeURIComponent(search)}`,
    "limit=200",
  ].filter(Boolean).join("&");

  const { data: apps, isLoading } = useQuery({
    queryKey: ["apps", statusFilter, search],
    queryFn: () => api.listApps(params || "limit=200"),
    refetchInterval: 15_000,
  });

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-slate-100">Applications</h1>
        <p className="text-sm text-slate-500 mt-0.5">{apps?.length ?? 0} records</p>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-3 mb-5">
        {/* Status tabs */}
        <div className="flex bg-slate-800/60 border border-slate-700 rounded-lg p-0.5 gap-0.5">
          {STATUS_TABS.map(({ key, label }) => (
            <button
              key={key}
              onClick={() => setStatusFilter(key)}
              className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
                statusFilter === key
                  ? "bg-blue-600 text-white"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {/* Search */}
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-500" />
          <input
            type="text"
            placeholder="Filter by company…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-8 pr-3 py-1.5 text-sm bg-slate-800 border border-slate-700 rounded-lg text-slate-300 placeholder-slate-600 focus:outline-none focus:border-blue-500 w-48"
          />
        </div>
      </div>

      {/* Table */}
      <div className="bg-slate-800/40 border border-slate-700 rounded-xl overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-slate-500 uppercase tracking-wider border-b border-slate-700 bg-slate-900/40">
                <th className="px-4 py-3 text-left">Company</th>
                <th className="px-4 py-3 text-left">Role</th>
                <th className="px-4 py-3 text-center">Status</th>
                <th className="px-4 py-3 text-center">Score</th>
                <th className="px-4 py-3 text-left">ATS</th>
                <th className="px-4 py-3 text-left">Updated</th>
                <th className="px-4 py-3"></th>
              </tr>
            </thead>
            <tbody>
              {isLoading ? (
                <tr><td colSpan={7} className="px-4 py-8 text-center text-slate-600">Loading…</td></tr>
              ) : !apps?.length ? (
                <tr><td colSpan={7} className="px-4 py-8 text-center text-slate-600">No applications found</td></tr>
              ) : (
                apps.map((app) => (
                  <tr key={app.id} className="border-b border-slate-800/80 hover:bg-slate-700/20 transition-colors">
                    <td className="px-4 py-3 text-slate-200 font-medium">{app.company_name ?? "—"}</td>
                    <td className="px-4 py-3 text-slate-300 max-w-[280px]">
                      <div className="truncate">{app.role_title ?? "—"}</div>
                    </td>
                    <td className="px-4 py-3 text-center">
                      <span className={`text-xs px-2 py-0.5 rounded-full border ${STATUS_COLOR[app.status] ?? "bg-slate-500/20 text-slate-400 border-slate-500/40"}`}>
                        {STATUS_LABEL[app.status] ?? app.status}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-center"><ScoreBadge score={app.quality_score} /></td>
                    <td className="px-4 py-3">
                      {app.ats_url ? (
                        <a href={app.ats_url} target="_blank" rel="noreferrer"
                          className="flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300">
                          <ExternalLink className="w-3 h-3 shrink-0" />
                          <span className="truncate max-w-[160px]">
                            {(() => { try { return new URL(app.ats_url).hostname; } catch { return app.ats_url; } })()}
                          </span>
                        </a>
                      ) : "—"}
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-500">{fmtDate(app.updated_at)}</td>
                    <td className="px-4 py-3">
                      <button
                        onClick={() => setProofId(app.id)}
                        className="flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300 bg-blue-500/10 border border-blue-500/30 rounded px-2 py-1"
                      >
                        <Eye className="w-3 h-3" /> View
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {proofId && <ProofModal appId={proofId} onClose={() => setProofId(null)} />}

      {/* Blocker details for blocked tab */}
      {statusFilter === "blocked" && (apps?.length ?? 0) > 0 && (
        <div className="mt-6 space-y-3">
          <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider">Blocker Breakdown</h2>
          {Object.entries(
            apps!.reduce<Record<string, number>>((acc, app) => {
              const key = app.rejection_reason?.split("\n")[0]?.slice(0, 80) ?? "Unknown";
              acc[key] = (acc[key] ?? 0) + 1;
              return acc;
            }, {})
          ).sort((a, b) => b[1] - a[1]).map(([reason, count]) => (
            <div key={reason} className="flex items-center gap-3 bg-slate-800/40 border border-slate-700 rounded-lg px-4 py-3">
              <span className="text-lg font-bold text-red-400 w-8 text-center">{count}</span>
              <span className="text-sm text-slate-300">{reason}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
