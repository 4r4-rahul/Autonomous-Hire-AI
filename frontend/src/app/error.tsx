"use client";
// App Router error boundary — catches render/runtime errors in any page so one bad row
// (e.g. a malformed ats_url) shows a recoverable message instead of a blank screen.
import { useEffect } from "react";

export default function Error({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.error("[AutonomousHire] page error:", error);
  }, [error]);

  return (
    <div className="flex flex-col items-center justify-center py-24 text-center">
      <div className="text-3xl mb-3">⚠️</div>
      <h2 className="text-lg font-semibold text-slate-100">Something went wrong on this page</h2>
      <p className="text-sm text-slate-500 mt-1 max-w-md">
        {error?.message || "An unexpected error occurred."} The rest of the app is fine.
      </p>
      <button
        onClick={reset}
        className="mt-5 bg-blue-600 hover:bg-blue-500 text-white text-sm font-semibold px-4 py-2 rounded-lg transition-colors"
      >
        Try again
      </button>
    </div>
  );
}
