import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, getChapters, getJob } from "../api/client";
import { isTerminal } from "../types";
import type { ChapterInfo, JobDetail } from "../types";

const POLL_INTERVAL_MS = 2000;
const MAX_BACKOFF_MS = 30_000;

export interface PollingState {
  job: JobDetail | null;
  chapters: ChapterInfo[];
  /** First load hasn't finished yet. */
  loading: boolean;
  /** The job doesn't exist (or is no longer available). */
  notFound: boolean;
  /** Fatal, non-network error (e.g. unexpected response). */
  error: ApiError | null;
  /** Temporary connection trouble; polling continues with backoff. */
  connectionIssue: boolean;
  /** Fetch immediately and resume polling (after cancel/retry). */
  refresh: () => void;
}

/**
 * Polls the job (and its chapters) about every 2 seconds while it is not in a
 * terminal state, backs off when the server is unreachable, and stops once the
 * job finishes. Reloading the page simply loads the existing job again.
 */
export function useJobPolling(jobId: string): PollingState {
  const [job, setJob] = useState<JobDetail | null>(null);
  const [chapters, setChapters] = useState<ChapterInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [connectionIssue, setConnectionIssue] = useState(false);
  const [generation, setGeneration] = useState(0);
  const timer = useRef<number | undefined>(undefined);

  const refresh = useCallback(() => setGeneration((g) => g + 1), []);

  useEffect(() => {
    let cancelled = false;
    let failures = 0;
    setLoading(true);
    setNotFound(false);
    setError(null);

    const schedule = (delay: number) => {
      timer.current = window.setTimeout(tick, delay);
    };

    async function tick() {
      try {
        const [nextJob, nextChapters] = await Promise.all([
          getJob(jobId),
          getChapters(jobId),
        ]);
        if (cancelled) return;
        failures = 0;
        setConnectionIssue(false);
        setError(null);
        setJob(nextJob);
        setChapters(nextChapters);
        setLoading(false);
        if (!isTerminal(nextJob.status)) schedule(POLL_INTERVAL_MS);
      } catch (caught) {
        if (cancelled) return;
        const apiError =
          caught instanceof ApiError
            ? caught
            : new ApiError("Something went wrong.", {
                status: 0,
                code: "unknown",
                kind: "network",
              });

        if (apiError.isNotFound || apiError.isGone) {
          setNotFound(true);
          setLoading(false);
          return;
        }
        if (apiError.kind === "invalid_response") {
          setError(apiError);
          setLoading(false);
          return;
        }
        // Network trouble or a server error: keep the job on screen, retry
        // with exponential backoff.
        failures += 1;
        setConnectionIssue(true);
        setLoading(false);
        schedule(Math.min(POLL_INTERVAL_MS * 2 ** failures, MAX_BACKOFF_MS));
      }
    }

    void tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer.current);
    };
  }, [jobId, generation]);

  return { job, chapters, loading, notFound, error, connectionIssue, refresh };
}
