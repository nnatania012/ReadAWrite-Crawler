import { useEffect } from "react";
import { startDownload } from "../api/client";
import {
  clearAutoDownloaded,
  markAutoDownloaded,
  wasAutoDownloaded,
} from "../lib/storage";
import { isTerminal } from "../types";
import type { JobDetail } from "../types";

/**
 * Attempts the ZIP download exactly once per finished run.
 *
 * The "already attempted" flag is written *before* the attempt, so polling,
 * re-renders and React StrictMode can never trigger a second download. The
 * flag is cleared when the job becomes active again (e.g. after a resume) so
 * the next completion downloads once more. Browsers may still block the
 * automatic download, which is why the page always shows a manual button.
 */
export function useAutoDownload(
  job: JobDetail | null,
  onFailure: (message: string) => void,
): void {
  const jobId = job?.job_id;
  const status = job?.status;
  const ready = job?.download_ready;

  useEffect(() => {
    if (!jobId || !status) return;

    if (!isTerminal(status)) {
      clearAutoDownloaded(jobId);
      return;
    }
    if (!ready || wasAutoDownloaded(jobId)) return;

    markAutoDownloaded(jobId);
    startDownload(jobId).catch(() =>
      onFailure(
        "The automatic download didn't start. Use the Download All Chapters button.",
      ),
    );
    // onFailure is intentionally excluded: it must not retrigger a download.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, status, ready]);
}
