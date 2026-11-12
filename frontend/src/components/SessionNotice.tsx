import { useSessionNotice } from "./SessionProvider";
import { Notice } from "./Card";

/** Shown once when an expired session was replaced with a new one. */
export function SessionNotice() {
  const { notice, dismissNotice } = useSessionNotice();
  if (!notice) return null;
  return (
    <div className="space-y-2">
      <Notice tone="info" title="New session">
        {notice}
      </Notice>
      <button
        onClick={dismissNotice}
        className="text-xs text-slate-400 underline hover:text-white"
      >
        Dismiss
      </button>
    </div>
  );
}
