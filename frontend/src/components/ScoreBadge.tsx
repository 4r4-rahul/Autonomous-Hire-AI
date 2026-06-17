// Shared score badge — single source of truth for the quality-score color thresholds
// (was duplicated across approvals/applications/linkedin with inconsistent cutoffs: 80 vs 75).
export function ScoreBadge({ score, fit }: { score: number | null; fit?: number | null }) {
  // Show the reviewed quality score; if there isn't one yet (cheap-parked / not tailored), fall
  // back to the quick intake FIT score, marked preliminary (dashed) so it's clearly not the
  // full review.
  const preliminary = score == null && fit != null;
  const s = score ?? fit ?? null;
  if (s == null) return null;
  const bg =
    s >= 80 ? "bg-green-500/20 text-green-400 border-green-500/40"
    : s >= 65 ? "bg-yellow-500/20 text-yellow-400 border-yellow-500/40"
    : "bg-red-500/20 text-red-400 border-red-500/40";
  return (
    <span
      title={preliminary ? "Preliminary fit score — full quality review runs when this role is tailored" : "Quality score"}
      className={`text-xs font-bold px-2 py-0.5 rounded-full border ${bg} ${preliminary ? "border-dashed opacity-80" : ""}`}>
      {s}/100{preliminary ? " · fit" : ""}
    </span>
  );
}
