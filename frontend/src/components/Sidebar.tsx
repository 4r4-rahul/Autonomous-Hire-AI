"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { api } from "@/lib/api";
import { dedupeContract } from "@/lib/queue";
import { cn } from "@/lib/utils";
import {
  LayoutDashboard, Network, ClipboardList, Bell, Activity, Brain, Play, Square, Globe, Handshake
} from "lucide-react";

const NAV = [
  { href: "/",             label: "Dashboard",    icon: LayoutDashboard },
  { href: "/linkedin",     label: "LinkedIn",     icon: Network },
  { href: "/applications", label: "Applications", icon: ClipboardList },
  { href: "/approvals",    label: "Review Queue", icon: Bell },
  { href: "/contract",     label: "Contract (C2C)", icon: Handshake },
  { href: "/india",        label: "India Roles",  icon: Globe },
  { href: "/insights",     label: "Insights",     icon: Brain },
  { href: "/activity",     label: "Activity",     icon: Activity },
];

export function Sidebar() {
  const path = usePathname();
  const { data: status } = useQuery({ queryKey: ["pipeline-status"], queryFn: api.pipelineStatus, refetchInterval: 3000 });
  const { data: stats }  = useQuery({ queryKey: ["stats"],           queryFn: api.stats });

  // Contract badge: fetch ONLY contract rows server-side (matches the /contract desk exactly,
  // including vendor-dedup) — the paused W2 backlog can't cap it. Review badge = total pending − contract.
  const { data: contractPending } = useQuery({
    queryKey: [...["apps-pending-approvals"], "contract"],
    queryFn: () => api.listApps("status=pending_approval&employment_type=contract&limit=400"),
    refetchInterval: 15_000,
  });
  const navCounts = useMemo(() => {
    const contract = dedupeContract(contractPending ?? []).length;
    const totalPending = stats?.pending_review ?? 0;
    return { contract, review: Math.max(0, totalPending - (contractPending?.length ?? 0)) };
  }, [contractPending, stats]);

  const running = status?.running ?? false;

  const handleStart = async () => { await api.startPipeline("linkedin"); };
  const handleStop  = async () => { await api.stopPipeline(); };

  return (
    <aside className="w-64 shrink-0 border-r border-slate-800 bg-[#0a1628] flex flex-col">
      {/* Logo */}
      <div className="px-5 py-5 border-b border-slate-800">
        <div className="text-lg font-bold text-slate-100 tracking-tight">🤖 AutonomousHire</div>
        <div className="text-xs text-slate-500 mt-0.5">AI Job Application Pipeline</div>
      </div>

      {/* Status pill */}
      <div className="px-5 py-3 border-b border-slate-800 flex items-center gap-2">
        <span className={cn("w-2 h-2 rounded-full", running ? "bg-green-400 animate-pulse" : "bg-slate-600")} />
        <span className={cn("text-xs font-medium", running ? "text-green-400" : "text-slate-500")}>
          {running ? `Running — ${status?.current_step ?? "…"}` : "Idle"}
        </span>
      </div>

      {/* Nav */}
      <nav className="flex-1 px-3 py-4 space-y-1">
        {NAV.map(({ href, label, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            className={cn(
              "flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors",
              path === href
                ? "bg-blue-600/20 text-blue-400 font-medium"
                : "text-slate-400 hover:bg-slate-800 hover:text-slate-100"
            )}
          >
            <Icon className="w-4 h-4 shrink-0" />
            <span>{label}</span>
            {label === "Review Queue" && navCounts.review > 0 && (
              <span className="ml-auto text-xs bg-yellow-500/20 text-yellow-400 border border-yellow-500/40 rounded-full px-2 py-0.5">
                {navCounts.review}
              </span>
            )}
            {label === "Contract (C2C)" && navCounts.contract > 0 && (
              <span className="ml-auto text-xs bg-amber-500/20 text-amber-400 border border-amber-500/40 rounded-full px-2 py-0.5">
                {navCounts.contract}
              </span>
            )}
          </Link>
        ))}
      </nav>

      {/* Pipeline controls */}
      <div className="px-4 py-4 border-t border-slate-800 space-y-2">
        <div className="text-xs font-semibold text-slate-600 uppercase tracking-wider mb-2">Pipeline</div>
        {!running ? (
          <button
            onClick={handleStart}
            className="w-full flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium py-2 rounded-lg transition-colors"
          >
            <Play className="w-3.5 h-3.5" /> Run LinkedIn
          </button>
        ) : (
          <button
            onClick={handleStop}
            className="w-full flex items-center justify-center gap-2 bg-red-600/20 hover:bg-red-600/30 border border-red-600/40 text-red-400 text-sm font-medium py-2 rounded-lg transition-colors"
          >
            <Square className="w-3.5 h-3.5" /> Stop Pipeline
          </button>
        )}
      </div>

      {/* Quick stats */}
      {stats && (
        <div className="px-4 pb-5 space-y-1 text-xs">
          {[
            ["Queue",     stats.queued_jobs,        "text-indigo-400"],
            ["Submitted", stats.submitted,           "text-green-400"],
            ["Pending",   stats.pending_review,      "text-yellow-400"],
            ["Blocked",   stats.blocked,             "text-red-400"],
          ].map(([label, val, color]) => (
            <div key={label as string} className="flex justify-between">
              <span className="text-slate-600">{label}</span>
              <span className={cn("font-semibold", color)}>{val}</span>
            </div>
          ))}
        </div>
      )}
    </aside>
  );
}
