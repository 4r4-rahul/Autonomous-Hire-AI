"use client";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { fmtDate } from "@/lib/utils";
import { X, ExternalLink, CheckCircle, AlertCircle, Check } from "lucide-react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";
const STATIC_BASE = API_BASE.replace("/api/v1", "");

interface Props {
  appId: string;
  onClose: () => void;
  onDecide?: (action: "approve" | "reject", reason?: string) => void;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-6">
      <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">{title}</h3>
      {children}
    </div>
  );
}

function QualityDimensions({ report }: { report: Record<string, unknown> }) {
  const dims = report.dimension_scores as Record<string, number> | undefined;
  const deductions = report.top_3_deductions as Array<{ dimension: string; points_lost: number; reason: string }> | undefined;

  return (
    <div className="space-y-3">
      {dims && (
        <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
          {Object.entries(dims).map(([k, v]) => (
            <div key={k} className="bg-slate-900/60 rounded-lg px-3 py-2">
              <div className="text-xs text-slate-500 capitalize">{k.replace(/_/g, " ")}</div>
              <div className="text-lg font-bold text-slate-200">{v}</div>
            </div>
          ))}
        </div>
      )}
      {deductions && deductions.length > 0 && (
        <div className="space-y-2 mt-2">
          <div className="text-xs text-slate-500 uppercase tracking-wider">Deductions</div>
          {deductions.map((d, i) => (
            <div key={i} className="bg-red-500/5 border border-red-500/20 rounded-lg p-3 text-xs">
              <span className="text-red-400 font-semibold capitalize">{d.dimension.replace(/_/g, " ")} −{d.points_lost}pts</span>
              <p className="text-slate-400 mt-1 leading-relaxed">{d.reason}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function ProofModal({ appId, onClose, onDecide }: Props) {
  const { data: proof, isLoading, error } = useQuery({
    queryKey: ["proof", appId],
    queryFn: () => api.getAppProof(appId),
    staleTime: Infinity,
  });

  const canDecide = !!onDecide && (proof?.status === "pending_approval" || proof?.status === "reviewing");

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-end">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />

      {/* Drawer */}
      <div className="relative w-full max-w-3xl h-full bg-[#0a1628] border-l border-slate-700 overflow-y-auto shadow-2xl">
        {/* Header */}
        <div className="sticky top-0 z-10 bg-[#0a1628] border-b border-slate-700 px-6 py-4 flex items-center justify-between">
          <div>
            <h2 className="text-lg font-bold text-slate-100">
              {isLoading ? "Loading…" : proof ? `${proof.company} — ${proof.role}` : "Proof of Submission"}
            </h2>
            {proof && (
              <p className="text-xs text-slate-500 mt-0.5">
                Submitted {fmtDate(proof.submitted_at)} · ATS: {proof.ats}
              </p>
            )}
          </div>
          <div className="flex items-center gap-2">
            {canDecide && (
              <>
                {proof?.ats_url && (
                  <a
                    href={proof.ats_url} target="_blank" rel="noreferrer"
                    className="flex items-center gap-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold px-3 py-1.5 rounded-lg transition-colors"
                  >
                    Open &amp; Apply <ExternalLink className="w-3.5 h-3.5" />
                  </a>
                )}
                <button
                  onClick={() => onDecide!("approve")}
                  className="flex items-center gap-1.5 bg-green-600/20 hover:bg-green-600/40 border border-green-500/40 text-green-400 text-xs font-semibold px-3 py-1.5 rounded-lg transition-colors"
                >
                  <Check className="w-3.5 h-3.5" /> Mark Applied
                </button>
                <button
                  onClick={() => onDecide!("reject", window.prompt("Reason for skipping (optional):") ?? undefined)}
                  className="flex items-center gap-1.5 bg-red-600/10 hover:bg-red-600/30 border border-red-500/40 text-red-400 text-xs font-semibold px-3 py-1.5 rounded-lg transition-colors"
                >
                  <X className="w-3.5 h-3.5" /> Skip
                </button>
              </>
            )}
            <button onClick={onClose} className="p-2 rounded-lg text-slate-500 hover:text-slate-300 hover:bg-slate-800 transition-colors">
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        <div className="px-6 py-6">
          {isLoading && (
            <div className="flex items-center justify-center h-40 text-slate-600">Loading proof…</div>
          )}

          {error && (
            <div className="bg-red-500/10 border border-red-500/30 rounded-xl p-4 text-red-400 text-sm">
              Failed to load proof data.
            </div>
          )}

          {proof && (
            <>
              {/* Submission status */}
              <Section title="Submission">
                <div className="grid grid-cols-2 gap-3 text-sm">
                  <div className="bg-slate-800/50 border border-slate-700 rounded-lg p-3">
                    <div className="text-xs text-slate-500 mb-1">Status</div>
                    <div className="flex items-center gap-1.5">
                      {proof.status === "submitted"
                        ? <CheckCircle className="w-3.5 h-3.5 text-green-400" />
                        : <AlertCircle className="w-3.5 h-3.5 text-yellow-400" />
                      }
                      <span className={`font-semibold ${proof.status === "submitted" ? "text-green-400" : "text-yellow-400"}`}>
                        {proof.status.replace(/_/g, " ")}
                      </span>
                    </div>
                  </div>
                  <div className="bg-slate-800/50 border border-slate-700 rounded-lg p-3">
                    <div className="text-xs text-slate-500 mb-1">Quality Score</div>
                    <div className={`text-xl font-bold ${
                      (proof.quality_score ?? 0) >= 80 ? "text-green-400"
                      : (proof.quality_score ?? 0) >= 65 ? "text-yellow-400"
                      : "text-red-400"
                    }`}>{proof.quality_score ?? "—"}/100</div>
                  </div>
                </div>

                {proof.ats_url && (
                  <a href={proof.ats_url} target="_blank" rel="noreferrer"
                    className="mt-3 flex items-center gap-2 text-xs text-blue-400 hover:text-blue-300 bg-blue-500/10 border border-blue-500/30 rounded-lg px-3 py-2 w-fit">
                    <ExternalLink className="w-3.5 h-3.5 shrink-0" />
                    <span className="truncate max-w-[400px]">{proof.ats_url}</span>
                  </a>
                )}
                {proof.confirmation_url && proof.confirmation_url !== proof.ats_url && (
                  <a href={proof.confirmation_url} target="_blank" rel="noreferrer"
                    className="mt-2 flex items-center gap-2 text-xs text-green-400 hover:text-green-300 bg-green-500/10 border border-green-500/30 rounded-lg px-3 py-2 w-fit">
                    <CheckCircle className="w-3.5 h-3.5 shrink-0" />
                    <span className="truncate max-w-[400px]">Confirmation: {proof.confirmation_url}</span>
                  </a>
                )}
              </Section>

              {/* Screenshots */}
              {(proof.filled_screenshot_url || proof.confirmation_screenshot_url) && (
                <Section title="Screenshots">
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    {proof.filled_screenshot_url && (
                      <div>
                        <div className="text-xs text-slate-500 mb-2">Form filled (before submit)</div>
                        <a href={`${STATIC_BASE}${proof.filled_screenshot_url}`} target="_blank" rel="noreferrer">
                          <img
                            src={`${STATIC_BASE}${proof.filled_screenshot_url}`}
                            alt="Filled form"
                            className="w-full rounded-lg border border-slate-700 hover:border-blue-500 transition-colors cursor-zoom-in"
                          />
                        </a>
                      </div>
                    )}
                    {proof.confirmation_screenshot_url && (
                      <div>
                        <div className="text-xs text-slate-500 mb-2">Confirmation page</div>
                        <a href={`${STATIC_BASE}${proof.confirmation_screenshot_url}`} target="_blank" rel="noreferrer">
                          <img
                            src={`${STATIC_BASE}${proof.confirmation_screenshot_url}`}
                            alt="Confirmation"
                            className="w-full rounded-lg border border-slate-700 hover:border-green-500 transition-colors cursor-zoom-in"
                          />
                        </a>
                      </div>
                    )}
                  </div>
                  {proof.debug_screenshot_url && (
                    <a href={`${STATIC_BASE}${proof.debug_screenshot_url}`} target="_blank" rel="noreferrer"
                      className="mt-2 text-xs text-slate-500 hover:text-slate-400 underline block">
                      View debug screenshot
                    </a>
                  )}
                </Section>
              )}

              {/* Quality report */}
              {proof.quality_report && (
                <Section title="Quality Report">
                  <QualityDimensions report={proof.quality_report} />
                </Section>
              )}

              {/* Tailored resume */}
              {proof.resume_markdown && (
                <Section title="Tailored Resume Used">
                  <div className="flex gap-2 mb-3">
                    <a href={`${API_BASE}/applications/${appId}/resume.pdf`} target="_blank" rel="noreferrer"
                      className="inline-flex items-center gap-1.5 text-xs font-medium text-blue-300 bg-blue-500/10 border border-blue-500/30 rounded-lg px-3 py-1.5 hover:bg-blue-500/20">
                      <ExternalLink size={13} /> View résumé (PDF)
                    </a>
                    <a href={`${API_BASE}/applications/${appId}/resume.pdf?download=true`}
                      className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-300 bg-slate-700/40 border border-slate-600 rounded-lg px-3 py-1.5 hover:bg-slate-700/70">
                      ⬇ Download PDF
                    </a>
                  </div>
                  <div className="bg-slate-900/60 border border-slate-700 rounded-xl p-5 max-h-96 overflow-y-auto">
                    <pre className="text-xs text-slate-300 whitespace-pre-wrap font-mono leading-relaxed">
                      {proof.resume_markdown}
                    </pre>
                  </div>
                </Section>
              )}

              {/* Cover letter (only when this role has one — covers are selective) */}
              {proof.cover_letter_markdown && (
                <Section title="Cover Letter Used">
                  <div className="bg-slate-900/60 border border-slate-700 rounded-xl p-5 max-h-96 overflow-y-auto">
                    <pre className="text-xs text-slate-300 whitespace-pre-wrap font-sans leading-relaxed">
                      {proof.cover_letter_markdown}
                    </pre>
                  </div>
                </Section>
              )}

              {/* Job description */}
              {proof.job_description && (
                <Section title="Job Description">
                  <div className="bg-slate-900/60 border border-slate-700 rounded-xl p-5 max-h-48 overflow-y-auto">
                    <p className="text-xs text-slate-400 whitespace-pre-wrap leading-relaxed">
                      {proof.job_description}
                    </p>
                  </div>
                </Section>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
