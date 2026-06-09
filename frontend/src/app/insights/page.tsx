"use client";
import { useQuery } from "@tanstack/react-query";
import { api, type InsightCell, type Insights } from "@/lib/api";
import { fmtDate } from "@/lib/utils";
import { Brain, TrendingUp, MessageSquare, Ghost, Clock, AlertTriangle } from "lucide-react";

function pct(n: number) {
  return `${Math.round((n ?? 0) * 100)}%`;
}

function StatCard({ label, value, sub, icon: Icon, color }: {
  label: string; value: string | number; sub?: string;
  icon: React.ComponentType<{ className?: string }>; color: string;
}) {
  return (
    <div className="bg-slate-800/40 border border-slate-700 rounded-xl p-4">
      <div className="flex items-center gap-2 text-slate-500 text-xs font-medium uppercase tracking-wider">
        <Icon className={`w-4 h-4 ${color}`} /> {label}
      </div>
      <div className="mt-2 text-2xl font-bold text-slate-100">{value}</div>
      {sub && <div className="text-xs text-slate-500 mt-0.5">{sub}</div>}
    </div>
  );
}

function ConversionTable({ title, label, data }: {
  title: string; label: string; data: Record<string, InsightCell>;
}) {
  const rows = Object.entries(data)
    .filter(([k]) => k !== "?" && k !== "other")
    .sort((a, b) =>
      (b[1].positive - a[1].positive) ||
      (b[1].responded - a[1].responded) ||
      (b[1].submitted - a[1].submitted))
    .slice(0, 15);

  if (rows.length === 0) {
    return (
      <div className="bg-slate-800/40 border border-slate-700 rounded-xl p-4">
        <h3 className="text-sm font-semibold text-slate-300 mb-2">{title}</h3>
        <p className="text-xs text-slate-600">No data yet.</p>
      </div>
    );
  }

  return (
    <div className="bg-slate-800/40 border border-slate-700 rounded-xl p-4 overflow-x-auto">
      <h3 className="text-sm font-semibold text-slate-300 mb-3">{title}</h3>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-slate-500 text-left border-b border-slate-700">
            <th className="pb-2 font-medium">{label}</th>
            <th className="pb-2 font-medium text-right">Sub</th>
            <th className="pb-2 font-medium text-right">Resp</th>
            <th className="pb-2 font-medium text-right">Intv/Off</th>
            <th className="pb-2 font-medium text-right">Ghost</th>
            <th className="pb-2 font-medium text-right">Resp %</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([k, g]) => {
            const rr = g.resolved ? g.responded / g.resolved : 0;
            return (
              <tr key={k} className="border-b border-slate-800/60 last:border-0">
                <td className="py-1.5 text-slate-200">{k}</td>
                <td className="py-1.5 text-right text-slate-400">{g.submitted}</td>
                <td className="py-1.5 text-right text-slate-400">{g.responded}</td>
                <td className="py-1.5 text-right font-semibold text-green-400">{g.positive || "—"}</td>
                <td className="py-1.5 text-right text-slate-500">{g.ghosted}</td>
                <td className="py-1.5 text-right text-slate-300">{g.resolved ? pct(rr) : "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function InsightsPage() {
  const { data, isLoading } = useQuery<Insights>({
    queryKey: ["insights"],
    queryFn: api.insights,
    refetchInterval: 30_000,
  });

  if (isLoading) {
    return <div className="text-slate-500 text-sm">Loading insights…</div>;
  }

  const t = data?.totals;
  const noSignal = !t?.has_response_signal;

  return (
    <div>
      <div className="mb-6 flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-100 flex items-center gap-2">
            <Brain className="w-6 h-6 text-purple-400" /> Learning Insights
          </h1>
          <p className="text-sm text-slate-500 mt-0.5">
            Outcome conversion analytics feeding fit-scoring
            {data?.generated_at && <> · updated {fmtDate(data.generated_at)}</>}
          </p>
        </div>
      </div>

      {data?.available === false && (
        <div className="bg-slate-800/40 border border-slate-700 rounded-xl p-6 text-sm text-slate-400">
          The learning loop hasn&apos;t produced a report yet. It runs on every email-monitor
          tick (capture → learn). Once outcomes are captured, conversion analytics appear here.
        </div>
      )}

      {data?.available && (
        <>
          {noSignal && (
            <div className="mb-5 flex items-start gap-3 bg-yellow-500/10 border border-yellow-500/30 rounded-xl p-4">
              <AlertTriangle className="w-5 h-5 text-yellow-400 shrink-0 mt-0.5" />
              <div className="text-sm text-yellow-200/90">
                <span className="font-semibold">No responses captured yet</span> — no learnings
                were written (can&apos;t distinguish real ghosting from the email monitor not
                having run). The tables below are informational only; verify email monitoring is active.
              </div>
            </div>
          )}

          <div className="grid grid-cols-2 lg:grid-cols-5 gap-3 mb-6">
            <StatCard label="Submitted" value={t!.submitted} icon={TrendingUp} color="text-blue-400" />
            <StatCard label="Responded" value={t!.responded} sub={pct(t!.response_rate)}
              icon={MessageSquare} color="text-green-400" />
            <StatCard label="Interviews/Offers" value={t!.interviews_or_offers} sub={pct(t!.interview_rate)}
              icon={TrendingUp} color="text-emerald-400" />
            <StatCard label="Ghosted" value={t!.ghosted} icon={Ghost} color="text-slate-400" />
            <StatCard label="Pending" value={t!.pending} icon={Clock} color="text-yellow-400" />
          </div>

          {data.learnings_written?.length > 0 && (
            <div className="mb-6 bg-purple-500/10 border border-purple-500/30 rounded-xl p-4">
              <h3 className="text-sm font-semibold text-purple-300 mb-2 flex items-center gap-2">
                <Brain className="w-4 h-4" /> Learnings written to memory (feed fit-scoring next run)
              </h3>
              <ul className="space-y-1">
                {data.learnings_written.map((l, i) => (
                  <li key={i} className="text-xs text-slate-300 flex gap-2">
                    <span className="text-purple-400">•</span> {l}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="grid lg:grid-cols-2 gap-4">
            <ConversionTable title="By company"   label="Company"  data={data.by_company} />
            <ConversionTable title="By role type" label="Role"     data={data.by_role} />
            <ConversionTable title="By fit band"  label="Fit band" data={data.by_fit_band} />
            <ConversionTable title="By ATS"       label="ATS"      data={data.by_ats} />
          </div>
        </>
      )}
    </div>
  );
}
