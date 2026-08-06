"use client";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { api } from "@/lib/api";
import { Bot, User } from "lucide-react";
import Whiteboard from "@/components/Whiteboard";

type Turn = { who: "interviewer" | "candidate"; text: string };

function MockInterview() {
  const params = useSearchParams();
  const company = params.get("company") ?? "";
  const role = params.get("role") ?? "";
  const rawMode = params.get("mode");
  const mode = rawMode === "system_design" || rawMode === "linux" ? rawMode : "behavioral";
  const isSD = mode === "system_design";
  const isLinux = mode === "linux";

  // Linux mode: pick ONE hidden root cause per session (the model, left alone, always chooses the cert
  // fault). The candidate doesn't see which — it's sent to the API every turn so the box stays consistent.
  const [fault] = useState(() => {
    const faults = [
      "port 80 is already bound by a stale/other process (an old apache2 or a leftover nginx master) — nginx can't bind",
      "the root filesystem is full — / is at 100%, 'No space left on device' (logs or a core dump filled it)",
      "inodes are exhausted on / — df -h shows free space but df -i shows 100% IUse from millions of tiny files",
      "an nginx config syntax error was introduced during maintenance — a bad/misspelled directive in a site config file",
      "the nginx unit was left masked (or disabled) after maintenance — systemctl won't start it until unmasked",
      "the OOM killer killed the nginx worker — memory is exhausted; dmesg shows 'Out of memory: Killed process … nginx'",
      "wrong ownership/permissions on the webroot or a referenced file — the nginx user gets 'Permission denied'",
      "a dangling symlink in sites-enabled points to a site config that was deleted from sites-available",
      "nginx itself is healthy but proxies to an upstream app/DB that's refusing connections — every request 502s",
    ];
    return faults[Math.floor(Math.random() * faults.length)];
  });

  const [turns, setTurns] = useState<Turn[]>([]);
  const [answer, setAnswer] = useState("");
  const [thinking, setThinking] = useState(false);
  const [started, setStarted] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const transcript = (extra: Turn[]) =>
    [...turns, ...extra]
      .map((t) => `${t.who === "interviewer" ? "Interviewer" : "Candidate"}: ${t.text}`)
      .join("\n");

  async function nextQuestion(extra: Turn[]) {
    setThinking(true);
    setError(null);
    try {
      const { question } = await api.mockInterviewer({ company, role, mode, history: transcript(extra), ...(isLinux ? { fault } : {}) });
      setTurns((prev) => [...prev, ...extra, { who: "interviewer", text: question }]);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Request failed");
      setTurns((prev) => [...prev, ...extra]);
    } finally {
      setThinking(false);
    }
  }

  function start() {
    setStarted(true);
    nextQuestion([]);
  }

  function send() {
    const t = answer.trim();
    if (!t || thinking) return;
    setAnswer("");
    nextQuestion([{ who: "candidate", text: t }]);
  }

  const title = isSD ? "📐 Mock System Design" : isLinux ? "🐧 Mock Linux Troubleshooting" : "🎯 Mock Interview";
  const blurb = isSD
    ? "a dedicated 60-min system-design round. A senior infra engineer poses one architecture problem and drills it end-to-end (scale, data model, deep dive, failure modes, tradeoffs). Sketch on the whiteboard and explain out loud or by typing."
    : isLinux
    ? "a hands-on Linux troubleshooting round. You've SSH'd into a broken box — type real shell commands and the interviewer returns the terminal output, revealing the fault as you narrow it down. Find it, fix it, verify it, then a live bash-scripting task. Reproduce → systemctl status → LOGS → resources → config → ports → perms."
    : "grounded in your experience bank. The AI plays the interviewer; answer out loud or by typing, and it follows up.";
  const header = (
    <div className="mb-4">
      <h1 className="text-2xl font-bold text-slate-100">{title}</h1>
      <p className="text-sm text-slate-400 mt-1">
        {role || "Role"}{company ? ` @ ${company}` : ""} — {blurb}
      </p>
    </div>
  );

  const conversation = !started ? (
    <button onClick={start} className="rounded-full px-6 py-2.5 font-semibold bg-sky-600 text-white hover:bg-sky-700">
      {isSD ? "Start system design round" : isLinux ? "SSH into the box" : "Start interview"}
    </button>
  ) : (
    <div className={isSD ? "flex flex-col h-full min-h-0" : ""}>
      <div className={`border border-slate-800 rounded-xl bg-slate-900/40 p-4 flex flex-col space-y-3 ${isSD ? "flex-1 min-h-0 overflow-y-auto" : "min-h-[300px]"}`}>
        {turns.map((t, i) => {
          // Linux mode: render the interviewer's terminal output as a monospace console block.
          if (isLinux && t.who === "interviewer") {
            return (
              <div key={i} className="flex justify-start w-full">
                <pre className="text-xs sm:text-[13px] leading-relaxed text-emerald-300 bg-black/70 border border-slate-700 rounded-lg px-4 py-3 max-w-full w-full overflow-x-auto whitespace-pre-wrap font-mono">{t.text}</pre>
              </div>
            );
          }
          return (
            <div key={i} className={t.who === "candidate" ? "flex justify-end" : "flex justify-start"}>
              <div className={`rounded-2xl px-4 py-3 inline-block max-w-[80%] shadow flex items-start gap-2 ${t.who === "candidate" ? "bg-slate-700/50" : "bg-sky-500/10 border border-sky-500/30"}`}>
                {t.who === "candidate" ? <User className="w-5 h-5 mt-0.5 text-blue-400 shrink-0" /> : <Bot className="w-5 h-5 mt-0.5 text-sky-400 shrink-0" />}
                <span className={`text-sm text-slate-100 whitespace-pre-wrap ${isLinux ? "font-mono" : ""}`}>{t.text}</span>
              </div>
            </div>
          );
        })}
        {thinking && <div className="text-sky-300 text-sm animate-pulse">interviewer is thinking…</div>}
        {error && <div className="text-red-400 text-sm">{error}</div>}
      </div>
      <form
        className="flex gap-2 mt-3"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <input
          value={answer}
          onChange={(e) => setAnswer(e.target.value)}
          placeholder={isSD ? "Explain your design…" : isLinux ? "Type a shell command (e.g. systemctl status nginx)…" : "Type your answer…"}
          className={`flex-1 border border-slate-700 rounded-full px-4 py-2 bg-slate-800 text-slate-100 focus:outline-none focus:ring focus:ring-sky-400 ${isLinux ? "font-mono text-sm" : ""}`}
        />
        <button type="submit" disabled={!answer.trim() || thinking} className="rounded-full px-6 py-2 font-semibold bg-sky-600 text-white hover:bg-sky-700 disabled:opacity-60">
          Send
        </button>
      </form>
    </div>
  );

  if (isSD) {
    return (
      <div className="max-w-[1500px] mx-auto">
        {header}
        <div className="flex flex-col lg:flex-row gap-4 h-[76vh]">
          <div className="lg:flex-1 min-h-[360px] h-full">
            <Whiteboard />
          </div>
          <div className="lg:w-[440px] flex flex-col min-h-0">{conversation}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-3xl mx-auto">
      {header}
      {conversation}
    </div>
  );
}

export default function MockPage() {
  // useSearchParams requires a Suspense boundary in the App Router.
  return (
    <Suspense fallback={<div className="text-slate-400">Loading…</div>}>
      <MockInterview />
    </Suspense>
  );
}
