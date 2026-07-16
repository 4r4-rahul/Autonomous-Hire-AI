"use client";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { api } from "@/lib/api";
import { Bot, User } from "lucide-react";

type Turn = { who: "interviewer" | "candidate"; text: string };

function MockInterview() {
  const params = useSearchParams();
  const company = params.get("company") ?? "";
  const role = params.get("role") ?? "";

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
      const { question } = await api.mockInterviewer({ company, role, history: transcript(extra) });
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

  return (
    <div className="max-w-3xl mx-auto">
      <div className="mb-4">
        <h1 className="text-2xl font-bold text-slate-100">🎯 Mock Interview</h1>
        <p className="text-sm text-slate-400 mt-1">
          {role || "Role"}{company ? ` @ ${company}` : ""} — grounded in your experience bank. The AI plays
          the interviewer; answer out loud or by typing, and it follows up.
        </p>
      </div>

      {!started ? (
        <button onClick={start} className="rounded-full px-6 py-2.5 font-semibold bg-sky-600 text-white hover:bg-sky-700">
          Start interview
        </button>
      ) : (
        <>
          <div className="border border-slate-800 rounded-xl bg-slate-900/40 p-4 flex flex-col space-y-3 min-h-[300px]">
            {turns.map((t, i) => (
              <div key={i} className={t.who === "candidate" ? "flex justify-end" : "flex justify-start"}>
                <div className={`rounded-2xl px-4 py-3 inline-block max-w-[80%] shadow flex items-start gap-2 ${t.who === "candidate" ? "bg-slate-700/50" : "bg-sky-500/10 border border-sky-500/30"}`}>
                  {t.who === "candidate" ? <User className="w-5 h-5 mt-0.5 text-blue-400 shrink-0" /> : <Bot className="w-5 h-5 mt-0.5 text-sky-400 shrink-0" />}
                  <span className="text-sm text-slate-100 whitespace-pre-wrap">{t.text}</span>
                </div>
              </div>
            ))}
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
              placeholder="Type your answer…"
              className="flex-1 border border-slate-700 rounded-full px-4 py-2 bg-slate-800 text-slate-100 focus:outline-none focus:ring focus:ring-sky-400"
            />
            <button type="submit" disabled={!answer.trim() || thinking} className="rounded-full px-6 py-2 font-semibold bg-sky-600 text-white hover:bg-sky-700 disabled:opacity-60">
              Send
            </button>
          </form>
        </>
      )}
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
