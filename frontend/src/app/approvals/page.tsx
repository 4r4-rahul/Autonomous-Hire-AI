"use client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Application } from "@/lib/api";
import { fmtRelative, STATUS_LABEL } from "@/lib/utils";
import { Eye, Check, X, ExternalLink, Sparkles, Users } from "lucide-react";
import { ProofModal } from "@/components/ProofModal";
import { ReferralModal } from "@/components/ReferralModal";
import { ScoreBadge } from "@/components/ScoreBadge";

const PENDING_KEY = ["apps-pending-approvals"];

// "posted Xd ago" — green within a week (best recency edge), amber up to 3 weeks.
function PostedBadge({ postedAt }: { postedAt: string | null }) {
  if (!postedAt) return null;
  const days = Math.max(0, Math.floor((Date.now() - new Date(postedAt).getTime()) / 86400000));
  const tone = days <= 7 ? "bg-green-500/15 text-green-400 border-green-500/30"
             : days <= 21 ? "bg-amber-500/15 text-amber-400 border-amber-500/30"
             : "bg-slate-600/15 text-slate-500 border-slate-600/30";
  const label = days === 0 ? "posted today" : `posted ${days}d ago`;
  return <span className={`text-xs px-2 py-0.5 rounded-full border ${tone} hidden md:inline-block`}>{label}</span>;
}

export default function ApprovalsPage() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [referralApp, setReferralApp] = useState<Application | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const qc = useQueryClient();

  const { data: pending, isLoading: pendingLoading } = useQuery({
    queryKey: PENDING_KEY,
    queryFn: () => api.listApps("status=pending_approval&sort_by=recency&limit=100"),
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
    // Optimistic: remove the row immediately so it doesn't linger/reappear under polling.
    onMutate: async ({ id }) => {
      setActionError(null);
      await qc.cancelQueries({ queryKey: PENDING_KEY });
      const prev = qc.getQueryData<Application[]>(PENDING_KEY);
      qc.setQueryData<Application[]>(PENDING_KEY, (old) => (old ?? []).filter((a) => a.id !== id));
      return { prev };
    },
    onError: (_err, _vars, ctx) => {
      // Roll back the optimistic removal and surface the failure (no more silent data loss).
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

  // Tailor-on-demand: generate the Opus résumé for a parked role only when the user chooses to
  // pursue it. Takes ~20-30s; the row updates (gets a score) when done.
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

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-slate-100">Review Queue</h1>
        <p className="text-sm text-slate-500 mt-0.5">
          <strong className="text-blue-400">Open &amp; Apply ↗</strong> opens the application page in a new tab — use the
          AutonomousHire extension there to fill it, then submit. Back here, mark it{" "}
          <strong className="text-green-400">Applied</strong> or <strong className="text-red-400">Skip</strong>.
        </p>
      </div>

      {actionError && (
        <div className="mb-4 flex items-center justify-between bg-red-600/15 border border-red-500/40 text-red-300 text-sm px-4 py-2.5 rounded-lg">
          <span>{actionError}</span>
          <button onClick={() => setActionError(null)} className="text-red-400 hover:text-red-200">✕</button>
        </div>
      )}

      {/* ── Awaiting approval ─────────────────────────────────────────── */}
      <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">
        Awaiting your approval {pending?.length ? `(${pending.length})` : ""}
      </h2>
      <div className="space-y-2 mb-8">
        {(pending ?? []).map((app) => (
          <div key={app.id}
            className="bg-slate-800/50 border border-slate-700 rounded-xl px-5 py-4 flex items-center justify-between hover:bg-slate-700/30 transition-colors">
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
              <ScoreBadge score={app.quality_score} />
              <PostedBadge postedAt={app.posted_at} />
            </div>
            <div className="flex items-center gap-2 shrink-0 ml-4">
              {app.quality_score == null && (
                <button
                  onClick={() => tailor.mutate(app.id)}
                  disabled={tailoringId === app.id}
                  title="Generate the tailored Opus résumé for this role"
                  className="flex items-center gap-1.5 bg-purple-600/20 hover:bg-purple-600/40 border border-purple-500/40 text-purple-300 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors disabled:opacity-50"
                >
                  <Sparkles className="w-3.5 h-3.5" /> {tailoringId === app.id ? "Tailoring…" : "Prepare résumé"}
                </button>
              )}
              <button
                onClick={() => openApply(app)}
                disabled={!app.ats_url}
                title={app.ats_url ?? "no application URL"}
                className="flex items-center gap-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold px-3 py-1.5 rounded-lg transition-colors disabled:opacity-40"
              >
                Open &amp; Apply <ExternalLink className="w-3.5 h-3.5" />
              </button>
              <button
                onClick={() => setReferralApp(app)}
                title="Find connections + get ready-to-send referral notes"
                className="flex items-center gap-1.5 bg-amber-600/15 hover:bg-amber-600/30 border border-amber-500/40 text-amber-300 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors"
              >
                <Users className="w-3.5 h-3.5" /> Referral
              </button>
              <button
                onClick={() => setSelectedId(app.id)}
                className="flex items-center gap-1.5 bg-slate-700/40 hover:bg-slate-700 border border-slate-600 text-slate-300 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors"
              >
                <Eye className="w-3.5 h-3.5" /> Proof
              </button>
              <button
                disabled={busyId === app.id}
                onClick={() => onApplied(app.id)}
                className="flex items-center gap-1.5 bg-green-600/20 hover:bg-green-600/40 border border-green-500/40 text-green-400 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors disabled:opacity-50"
              >
                <Check className="w-3.5 h-3.5" /> {busyId === app.id ? "…" : "Applied"}
              </button>
              <button
                disabled={busyId === app.id}
                onClick={() => onSkip(app.id)}
                className="flex items-center gap-1.5 bg-red-600/10 hover:bg-red-600/30 border border-red-500/40 text-red-400 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors disabled:opacity-50"
              >
                <X className="w-3.5 h-3.5" /> Skip
              </button>
            </div>
          </div>
        ))}

        {pendingLoading && !pending && (
          <div className="space-y-2">
            {[0, 1, 2].map((i) => (
              <div key={i} className="h-16 bg-slate-800/40 border border-slate-800 rounded-xl animate-pulse" />
            ))}
          </div>
        )}
        {!pendingLoading && !pending?.length && (
          <div className="text-center py-12 text-slate-600 bg-slate-800/30 border border-slate-800 rounded-xl">
            <div className="text-3xl mb-2">✅</div>
            <p className="text-sm">Nothing waiting for approval.</p>
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
              <ReadonlyRow key={app.id} app={app} onProof={() => setSelectedId(app.id)} onReferral={() => setReferralApp(app)} tone="teal" />
            ))}
          </div>
        </>
      )}

      {/* ── Submitted (confirmed) ─────────────────────────────────────── */}
      <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">
        Submitted {submitted?.length ? `(${submitted.length})` : ""}
      </h2>
      <div className="space-y-2">
        {(submitted ?? []).map((app) => (
          <ReadonlyRow key={app.id} app={app} onProof={() => setSelectedId(app.id)} onReferral={() => setReferralApp(app)} tone="green" />
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
          onDecide={(action, reason) => {
            decide.mutate({ id: selectedId, action, reason });
            setSelectedId(null);
          }}
        />
      )}

      {referralApp && (
        <ReferralModal
          appId={referralApp.id}
          company={referralApp.company_name || ""}
          role={referralApp.role_title || ""}
          onClose={() => setReferralApp(null)}
        />
      )}
    </div>
  );
}

function ReadonlyRow({ app, onProof, onReferral, tone }: { app: Application; onProof: () => void; onReferral?: () => void; tone: "teal" | "green" }) {
  const dot = tone === "teal" ? "bg-teal-400" : "bg-green-400";
  return (
    <div className="bg-slate-800/50 border border-slate-700 rounded-xl px-5 py-4 flex items-center justify-between hover:bg-slate-700/30 transition-colors">
      <div className="flex items-center gap-4 min-w-0">
        <span className={`w-2 h-2 rounded-full ${dot} shrink-0`} />
        <div className="min-w-0">
          <div className="text-sm font-semibold text-slate-100 truncate">{app.company_name}</div>
          <div className="text-xs text-slate-400 truncate mt-0.5">{app.role_title}</div>
        </div>
        <ScoreBadge score={app.quality_score} />
        <span className="text-xs text-slate-600 hidden md:block">
          {STATUS_LABEL[app.status] ?? app.status} · {fmtRelative(app.submitted_at ?? app.updated_at)}
        </span>
      </div>
      <div className="flex items-center gap-2 shrink-0 ml-4">
        {onReferral && (
          <button
            onClick={onReferral}
            title="Get ready-to-send referral notes + find connections — the biggest callback lever after applying"
            className="flex items-center gap-1.5 bg-amber-600/15 hover:bg-amber-600/30 border border-amber-500/40 text-amber-300 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors"
          >
            <Users className="w-3.5 h-3.5" /> Referral
          </button>
        )}
        <button
          onClick={onProof}
          className="flex items-center gap-2 bg-blue-600/20 hover:bg-blue-600/40 border border-blue-500/40 text-blue-400 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors"
        >
          <Eye className="w-3.5 h-3.5" /> View Proof
        </button>
      </div>
    </div>
  );
}
