"use client";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { X, ExternalLink, Copy, Check, Users } from "lucide-react";

interface Props {
  appId: string;
  company: string;
  role: string;
  onClose: () => void;
}

// A labelled, read-only message box with a copy button (select-and-copy, or one-click copy).
function CopyBox({ label, text, hint }: { label: string; text: string; hint?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — the user can still select the text manually */
    }
  };
  return (
    <div className="mb-5">
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">{label}</span>
        <button
          onClick={copy}
          disabled={!text}
          className="flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300 disabled:opacity-40"
        >
          {copied ? <><Check className="w-3.5 h-3.5" /> Copied</> : <><Copy className="w-3.5 h-3.5" /> Copy</>}
        </button>
      </div>
      {/* selectable text — click into it and Cmd+C also works if the button is flaky */}
      <textarea
        readOnly
        value={text || ""}
        onFocus={(e) => e.currentTarget.select()}
        rows={text && text.length > 160 ? 5 : 3}
        className="w-full resize-y bg-slate-900/70 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 leading-relaxed focus:outline-none focus:border-blue-500/50"
      />
      {hint && <p className="text-xs text-slate-600 mt-1">{hint}</p>}
    </div>
  );
}

export function ReferralModal({ appId, company, role, onClose }: Props) {
  // draft=true → Claude drafts a connection note + referral-ask (cheap Haiku). Cached per app.
  const { data: kit, isLoading, isError } = useQuery({
    queryKey: ["referral", appId],
    queryFn: () => api.referralKit(appId, true),
    staleTime: Infinity,
  });

  const open = (url?: string) => url && window.open(url, "_blank", "noopener");

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-end">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <div className="relative w-full max-w-xl h-full bg-[#0a1628] border-l border-slate-700 overflow-y-auto shadow-2xl">
        <div className="sticky top-0 z-10 bg-[#0a1628] border-b border-slate-700 px-6 py-4 flex items-center justify-between">
          <div className="min-w-0">
            <h2 className="text-lg font-bold text-slate-100 flex items-center gap-2">
              <Users className="w-4 h-4 text-amber-400" /> Get a referral
            </h2>
            <p className="text-xs text-slate-500 truncate mt-0.5">{company} — {role}</p>
          </div>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-300"><X className="w-5 h-5" /></button>
        </div>

        <div className="px-6 py-5">
          {/* Find-connections links */}
          <div className="mb-6 space-y-2">
            <button
              onClick={() => open(kit?.search_urls?.your_connections)}
              disabled={!kit?.search_urls?.your_connections}
              className="w-full flex items-center justify-between bg-amber-600/15 hover:bg-amber-600/30 border border-amber-500/40 text-amber-300 text-sm font-medium px-4 py-2.5 rounded-lg transition-colors disabled:opacity-40"
            >
              <span>Your 1st/2nd-degree connections at {company}</span> <ExternalLink className="w-4 h-4" />
            </button>
            <div className="flex gap-2">
              <button onClick={() => open(kit?.search_urls?.recruiters)} disabled={!kit?.search_urls?.recruiters}
                className="flex-1 flex items-center justify-center gap-1.5 bg-slate-700/40 hover:bg-slate-700 border border-slate-600 text-slate-300 text-xs px-3 py-2 rounded-lg disabled:opacity-40">
                Recruiters <ExternalLink className="w-3.5 h-3.5" />
              </button>
              <button onClick={() => open(kit?.search_urls?.team)} disabled={!kit?.search_urls?.team}
                className="flex-1 flex items-center justify-center gap-1.5 bg-slate-700/40 hover:bg-slate-700 border border-slate-600 text-slate-300 text-xs px-3 py-2 rounded-lg disabled:opacity-40">
                The team <ExternalLink className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>

          {/* Drafted messages */}
          {isLoading && (
            <div className="text-sm text-slate-500 flex items-center gap-2 py-6">
              <span className="w-3 h-3 rounded-full bg-amber-400 animate-pulse" /> Drafting your notes…
            </div>
          )}
          {isError && (
            <div className="text-sm text-red-400 py-4">
              Couldn&apos;t draft notes (check API/credits). The connection links above still work.
            </div>
          )}
          {kit && !isLoading && (
            <>
              <div className="text-xs text-slate-500 mb-3 leading-relaxed">
                Expert flow: <strong className="text-slate-400">1)</strong> connect with no ask ·{" "}
                <strong className="text-slate-400">2)</strong> after they accept, send the ask ·{" "}
                <strong className="text-slate-400">3)</strong> hand them the &ldquo;why-me&rdquo; so the
                referral takes 2 minutes.
              </div>

              <CopyBox
                label="① Connection note — no ask (≤300 chars)"
                text={kit.connect_note}
                hint="Personalize the [their work/post] bit (30s of research), then send with the connect request."
              />
              <CopyBox
                label="② Referral ask — send AFTER they accept"
                text={kit.referral_ask}
                hint="Low-pressure + offers your résumé so saying yes is effortless."
              />
              <CopyBox
                label="③ Why-me blurb — for THEM to paste into the referral form"
                text={kit.why_me}
                hint="Removes all friction: they paste this into the internal 'why are you referring them?' box."
              />
              <CopyBox
                label="④ Warm intro — if it's a 2nd-degree (ask the mutual)"
                text={kit.mutual_intro}
                hint="A mutual intro beats any cold note — use this when you share a connection."
              />

              {kit.job_url && (
                <a href={kit.job_url} target="_blank" rel="noreferrer"
                  className="inline-flex items-center gap-1.5 text-xs text-blue-400 hover:text-blue-300 mb-3">
                  <ExternalLink className="w-3.5 h-3.5" /> Job link to share with the referrer
                </a>
              )}
              <p className="text-xs text-slate-600">
                Tip: click into any box and the text auto-selects — then ⌘C, or use the Copy button.
                Then follow up once after ~4–5 days if no reply.
              </p>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
