import type { Progress } from "../types";

/**
 * Overall progress = chapters that reached a terminal state
 * (extracted + failed + partial + skipped) out of the total, straight from
 * the backend's numbers. The coloured segments show how they split.
 */
export function ProgressBar({ progress }: { progress: Progress }) {
  const { total, completed, failed, partial, skipped, processed } = progress;
  const pct = (n: number) => (total > 0 ? (n / total) * 100 : 0);
  const shown = total > 0 ? ((processed / total) * 100).toFixed(1) : "0.0";

  return (
    <div>
      <div className="mb-2 flex items-baseline justify-between text-sm">
        <span className="font-medium text-slate-200">
          {total > 0
            ? `${processed} / ${total} chapters processed`
            : "Waiting for the chapter list…"}
        </span>
        <span className="tabular-nums text-slate-400">{shown}%</span>
      </div>
      <div
        role="progressbar"
        aria-label="Extraction progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Number(shown)}
        className="flex h-3 w-full overflow-hidden rounded-full bg-slate-800"
      >
        <div className="bg-emerald-500 transition-all duration-500" style={{ width: `${pct(completed + skipped)}%` }} />
        <div className="bg-amber-500 transition-all duration-500" style={{ width: `${pct(partial)}%` }} />
        <div className="bg-rose-500 transition-all duration-500" style={{ width: `${pct(failed)}%` }} />
      </div>
    </div>
  );
}
