"use client";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { X, MessageSquareText } from "lucide-react";

interface Props {
  appId: string;
  company: string;
  role: string;
  onClose: () => void;
}

// Read-only record of the open-ended LLM answers drafted + cached for this application's forms.
export function FormAnswersModal({ appId, company, role, onClose }: Props) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["form-answers", appId],
    queryFn: () => api.getFormAnswers(appId),
    staleTime: 30_000,
  });

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-end">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <div className="relative w-full max-w-xl h-full bg-[#0a1628] border-l border-slate-700 overflow-y-auto shadow-2xl">
        <div className="sticky top-0 z-10 bg-[#0a1628] border-b border-slate-700 px-6 py-4 flex items-center justify-between">
          <div className="min-w-0">
            <h2 className="text-lg font-bold text-slate-100 flex items-center gap-2">
              <MessageSquareText className="w-4 h-4 text-sky-400" /> Application answers
            </h2>
            <p className="text-xs text-slate-500 truncate mt-0.5">{company} — {role}</p>
          </div>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-300"><X className="w-5 h-5" /></button>
        </div>

        <div className="px-6 py-5">
          {isLoading && <div className="text-sm text-slate-500 py-6">Loading…</div>}
          {isError && <div className="text-sm text-red-400 py-4">Couldn&apos;t load answers.</div>}
          {data && !isLoading && (
            data.count === 0 ? (
              <div className="text-sm text-slate-500 py-8 text-center">
                No saved answers yet. They&apos;re drafted (and cached here) the first time you open this
                role&apos;s application form with the extension.
              </div>
            ) : (
              <>
                <p className="text-xs text-slate-500 mb-4">
                  These were drafted for this role&apos;s form questions and reused on every re-open
                  (no re-generation). Click a box to select, then copy.
                </p>
                {data.answers.map((qa, i) => (
                  <div key={i} className="mb-5">
                    <div className="text-xs font-semibold text-slate-300 mb-1.5">{qa.question}</div>
                    <textarea
                      readOnly
                      value={qa.answer}
                      onFocus={(e) => e.currentTarget.select()}
                      rows={qa.answer.length > 160 ? 5 : 2}
                      className="w-full resize-y bg-slate-900/70 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 leading-relaxed focus:outline-none focus:border-sky-500/50"
                    />
                  </div>
                ))}
              </>
            )
          )}
        </div>
      </div>
    </div>
  );
}
