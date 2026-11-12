import { getSessionId } from "../lib/session";
import type {
  ApiErrorBody,
  ChapterInfo,
  CreateJobRequest,
  JobAccepted,
  JobDetail,
  JobSummary,
} from "../types";

export const API_BASE_URL: string = (
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000"
).replace(/\/+$/, "");

/** Fired when the backend says this session has ended (HTTP 410). */
export const SESSION_ENDED_EVENT = "rw:session-ended";

export type ApiErrorKind = "network" | "client" | "server" | "invalid_response";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly kind: ApiErrorKind;
  readonly extra: Record<string, unknown>;

  constructor(
    message: string,
    opts: {
      status: number;
      code: string;
      kind: ApiErrorKind;
      extra?: Record<string, unknown>;
    },
  ) {
    super(message);
    this.name = "ApiError";
    this.status = opts.status;
    this.code = opts.code;
    this.kind = opts.kind;
    this.extra = opts.extra ?? {};
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }

  /** 410: the session or the job's data has expired. */
  get isGone(): boolean {
    return this.status === 410;
  }

  get isSessionEnded(): boolean {
    return this.status === 410 && this.code === "session_expired";
  }
}

const isObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null;

function parseErrorBody(body: unknown): ApiErrorBody["error"] | null {
  if (!isObject(body) || !isObject(body.error)) return null;
  const { code, message, ...extra } = body.error;
  if (typeof code !== "string" || typeof message !== "string") return null;
  return { code, message, ...extra };
}

interface RequestOptions extends RequestInit {
  /** Tell the app when the backend reports the session ended (default). */
  notifySessionEnded?: boolean;
}

async function request(path: string, init: RequestOptions = {}): Promise<Response> {
  const { notifySessionEnded = true, ...fetchInit } = init;
  const headers = new Headers(fetchInit.headers);
  headers.set("X-Session-ID", getSessionId());

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, { ...fetchInit, headers });
  } catch {
    throw new ApiError(
      "Can't reach the server. Check that the backend is running.",
      { status: 0, code: "network_error", kind: "network" },
    );
  }

  if (response.ok) return response;

  let parsed: ApiErrorBody["error"] | null = null;
  try {
    parsed = parseErrorBody(await response.json());
  } catch {
    /* body wasn't JSON */
  }

  const { code, message, ...extra } = parsed ?? {
    code: "http_error",
    message:
      response.status >= 500
        ? "The server had a problem. Please try again."
        : `The request failed (HTTP ${response.status}).`,
  };

  const error = new ApiError(message, {
    status: response.status,
    code,
    kind: response.status >= 500 ? "server" : "client",
    extra,
  });

  if (notifySessionEnded && error.isSessionEnded) {
    window.dispatchEvent(new Event(SESSION_ENDED_EVENT));
  }
  throw error;
}

async function requestJson<T>(
  path: string,
  guard: (value: unknown) => value is T,
  init?: RequestOptions,
): Promise<T> {
  const response = await request(path, init);
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw invalidResponse();
  }
  if (!guard(body)) throw invalidResponse();
  return body;
}

const invalidResponse = () =>
  new ApiError("The server sent an unexpected response.", {
    status: 200,
    code: "invalid_response",
    kind: "invalid_response",
  });

// --- light runtime validation of what we rely on --------------------------

const isJobAccepted = (v: unknown): v is JobAccepted =>
  isObject(v) && typeof v.job_id === "string" && typeof v.status === "string";

const isJobDetail = (v: unknown): v is JobDetail =>
  isObject(v) &&
  typeof v.job_id === "string" &&
  typeof v.status === "string" &&
  isObject(v.progress) &&
  typeof v.progress.total === "number" &&
  typeof v.progress.processed === "number";

const isJobList = (v: unknown): v is { jobs: JobSummary[] } =>
  isObject(v) && Array.isArray(v.jobs);

const isChapterList = (v: unknown): v is { chapters: ChapterInfo[] } =>
  isObject(v) && Array.isArray(v.chapters);

export interface HeartbeatResult {
  session_id: string;
  status: string;
  heartbeat_seconds: number;
  idle_timeout_seconds: number;
}

const isHeartbeat = (v: unknown): v is HeartbeatResult =>
  isObject(v) &&
  typeof v.session_id === "string" &&
  typeof v.heartbeat_seconds === "number";

// --- sessions ---------------------------------------------------------------

export const sendHeartbeat = (): Promise<HeartbeatResult> =>
  requestJson("/api/sessions/heartbeat", isHeartbeat, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: getSessionId() }),
    notifySessionEnded: false, // the session provider handles this itself
  });

/**
 * Best-effort "this page is closing" notice. A text/plain body is a "simple"
 * request, so no CORS preflight is needed (which a closing page might never
 * get to finish). Browsers don't guarantee delivery; the backend also expires
 * idle sessions on its own.
 */
export function sendCloseNotice(): void {
  const url = `${API_BASE_URL}/api/sessions/close`;
  const body = JSON.stringify({ session_id: getSessionId() });
  try {
    const blob = new Blob([body], { type: "text/plain;charset=UTF-8" });
    if (typeof navigator.sendBeacon === "function" && navigator.sendBeacon(url, blob)) {
      return;
    }
  } catch {
    /* fall through to fetch */
  }
  try {
    void fetch(url, {
      method: "POST",
      body,
      keepalive: true,
      headers: { "Content-Type": "text/plain;charset=UTF-8" },
    }).catch(() => undefined);
  } catch {
    /* nothing more we can do while the page is closing */
  }
}

// --- jobs -------------------------------------------------------------------

const JSON_HEADERS = { "Content-Type": "application/json" };

export const createJob = (body: CreateJobRequest): Promise<JobAccepted> =>
  requestJson("/api/jobs", isJobAccepted, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });

export const listJobs = async (): Promise<JobSummary[]> =>
  (await requestJson("/api/jobs", isJobList)).jobs;

export const getJob = (jobId: string): Promise<JobDetail> =>
  requestJson(`/api/jobs/${encodeURIComponent(jobId)}`, isJobDetail);

export const getChapters = async (jobId: string): Promise<ChapterInfo[]> =>
  (
    await requestJson(
      `/api/jobs/${encodeURIComponent(jobId)}/chapters`,
      isChapterList,
    )
  ).chapters;

/**
 * One chapter's text, exactly as the backend sends it. The string is returned
 * untouched (no trimming, splitting, newline changes or escaping) so it can
 * go straight to navigator.clipboard.writeText.
 */
export async function getChapterContent(
  jobId: string,
  chapterNumber: number,
): Promise<string> {
  const response = await request(
    `/api/jobs/${encodeURIComponent(jobId)}/chapters/${chapterNumber}/content`,
    { cache: "no-store", headers: { Accept: "text/plain" } },
  );
  return response.text();
}

export const cancelJob = (jobId: string): Promise<JobAccepted> =>
  requestJson(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, isJobAccepted, {
    method: "POST",
  });

export const retryJob = (jobId: string): Promise<JobAccepted> =>
  requestJson(`/api/jobs/${encodeURIComponent(jobId)}/retry`, isJobAccepted, {
    method: "POST",
  });

/** A plain link can't send headers, so the session ID goes in the query. */
export const downloadUrl = (jobId: string): string =>
  `${API_BASE_URL}/api/jobs/${encodeURIComponent(jobId)}/download?session_id=${encodeURIComponent(getSessionId())}`;

/**
 * Download the ZIP. A HEAD request first confirms the archive is really
 * available (so errors can be shown), then the browser performs the actual
 * streamed download through a normal link click.
 */
export async function startDownload(jobId: string): Promise<void> {
  try {
    await request(`/api/jobs/${encodeURIComponent(jobId)}/download`, {
      method: "HEAD",
    });
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      throw new ApiError("The download isn't ready yet.", {
        status: 409,
        code: "archive_not_ready",
        kind: "client",
      });
    }
    throw error;
  }

  const link = document.createElement("a");
  link.href = downloadUrl(jobId);
  link.rel = "noopener";
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  link.remove();
}
