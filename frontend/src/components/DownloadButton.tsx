import { useState } from "react";
import { startDownload } from "../api/client";
import { buttonClass } from "./Card";

export function DownloadButton({
  jobId,
  className = buttonClass.success,
  label = "Download All Chapters",
}: {
  jobId: string;
  className?: string;
  label?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onClick() {
    setBusy(true);
    setError(null);
    try {
      await startDownload(jobId);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "The download failed.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="inline-flex flex-col items-start gap-1">
      <button type="button" onClick={onClick} disabled={busy} className={className}>
        <span aria-hidden>⬇</span>
        {busy ? "Preparing…" : label}
      </button>
      {error && (
        <span role="alert" className="text-xs text-rose-300">
          {error}
        </span>
      )}
    </span>
  );
}
