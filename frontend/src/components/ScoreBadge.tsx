// Shared score badge — single source of truth for the quality-score color thresholds
// (was duplicated across approvals/applications/linkedin with inconsistent cutoffs: 80 vs 75).
export function ScoreBadge({ score }: { score: number | null }) {
  if (score == null) return null;
  const bg =
    score >= 80 ? "bg-green-500/20 text-green-400 border-green-500/40"
    : score >= 65 ? "bg-yellow-500/20 text-yellow-400 border-yellow-500/40"
    : "bg-red-500/20 text-red-400 border-red-500/40";
  return <span className={`text-xs font-bold px-2 py-0.5 rounded-full border ${bg}`}>{score}/100</span>;
}
