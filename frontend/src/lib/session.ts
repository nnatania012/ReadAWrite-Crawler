/*
 * Browser-tab session identity.
 *
 * The session ID (a UUID) and the IDs of this session's jobs live in
 * sessionStorage, so they survive a refresh but vanish with the tab. A
 * duplicated tab copies sessionStorage, which would make two tabs share one
 * session (closing one would then clean up the other's jobs); a small
 * BroadcastChannel handshake detects that and gives the newcomer a new ID.
 */

import { clearAllSessionData } from "./storage";

const SESSION_KEY = "rw:sessionId";
const JOBS_KEY = "rw:sessionJobs";
const CHANNEL = "rw-session";
const MAX_TRACKED_JOBS = 50;

const TAB_ID = randomUuid();
let established = false;

function safe<T>(action: () => T, fallback: T): T {
  try {
    return action();
  } catch {
    return fallback;
  }
}

export function randomUuid(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** This tab's session ID (created on first use). */
export function getSessionId(): string {
  const existing = safe(() => sessionStorage.getItem(SESSION_KEY), null);
  if (existing) return existing;
  const fresh = randomUuid();
  safe(() => sessionStorage.setItem(SESSION_KEY, fresh), undefined);
  return fresh;
}

/** Start over with a new session ID and forget everything tied to the old one. */
export function resetSession(): string {
  clearAllSessionData();
  const fresh = randomUuid();
  safe(() => sessionStorage.setItem(SESSION_KEY, fresh), undefined);
  return fresh;
}

export function getSessionJobIds(): string[] {
  return safe(() => {
    const parsed: unknown = JSON.parse(sessionStorage.getItem(JOBS_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === "string") : [];
  }, []);
}

export function addSessionJobId(jobId: string): void {
  const ids = [jobId, ...getSessionJobIds().filter((id) => id !== jobId)].slice(0, MAX_TRACKED_JOBS);
  safe(() => sessionStorage.setItem(JOBS_KEY, JSON.stringify(ids)), undefined);
}

interface ClaimMessage {
  type: "who-has" | "i-have";
  sessionId: string;
  tabId: string;
}

const isClaim = (value: unknown): value is ClaimMessage =>
  typeof value === "object" &&
  value !== null &&
  "type" in value &&
  "sessionId" in value &&
  "tabId" in value;

let channel: BroadcastChannel | null = null;

/**
 * Make sure no other open tab is using this tab's session ID (which happens
 * when a tab is duplicated). Resolves true if a new session was started.
 * A page refresh is not a conflict: the old page is already gone.
 */
export function ensureUniqueSession(timeoutMs = 250): Promise<boolean> {
  if (typeof BroadcastChannel === "undefined") {
    established = true;
    return Promise.resolve(false);
  }

  const sessionId = getSessionId();
  channel?.close();
  const current = new BroadcastChannel(CHANNEL);
  channel = current;

  return new Promise((resolve) => {
    let taken = false;

    current.onmessage = (event: MessageEvent<unknown>) => {
      const message = event.data;
      if (!isClaim(message) || message.tabId === TAB_ID) return;

      if (message.type === "who-has" && message.sessionId === getSessionId()) {
        // An established tab always answers; two tabs starting at the same
        // moment are ordered by tab ID so exactly one of them keeps the ID.
        if (established || TAB_ID < message.tabId) {
          current.postMessage({ type: "i-have", sessionId: message.sessionId, tabId: TAB_ID });
        }
      } else if (message.type === "i-have" && message.sessionId === sessionId) {
        taken = true;
      }
    };

    current.postMessage({ type: "who-has", sessionId, tabId: TAB_ID });

    window.setTimeout(() => {
      if (taken) resetSession();
      established = true;
      resolve(taken);
    }, timeoutMs);
  });
}
