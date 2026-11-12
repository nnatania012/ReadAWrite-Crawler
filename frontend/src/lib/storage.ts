/*
 * Per-tab browser storage (sessionStorage): it disappears when the tab or
 * browser session ends and survives a page refresh. Only tiny, non-sensitive
 * values live here: the active job ID and a per-job flag recording that the
 * automatic ZIP download was already attempted. Never chapter text, ZIPs or
 * large API responses.
 */

const ACTIVE_KEY = "rw:activeJobId";
const AUTO_PREFIX = "rw:autoDownload:";

function safe<T>(action: () => T, fallback: T): T {
  try {
    return action();
  } catch {
    return fallback; // storage unavailable (private mode, quota, ...)
  }
}

export const getActiveJobId = (): string | null =>
  safe(() => sessionStorage.getItem(ACTIVE_KEY), null);

export const setActiveJobId = (jobId: string): void =>
  safe(() => sessionStorage.setItem(ACTIVE_KEY, jobId), undefined);

export const clearActiveJobId = (jobId?: string): void =>
  safe(() => {
    if (!jobId || sessionStorage.getItem(ACTIVE_KEY) === jobId) {
      sessionStorage.removeItem(ACTIVE_KEY);
    }
  }, undefined);

export const wasAutoDownloaded = (jobId: string): boolean =>
  safe(() => sessionStorage.getItem(AUTO_PREFIX + jobId) === "1", false);

export const markAutoDownloaded = (jobId: string): void =>
  safe(() => sessionStorage.setItem(AUTO_PREFIX + jobId, "1"), undefined);

export const clearAutoDownloaded = (jobId: string): void =>
  safe(() => sessionStorage.removeItem(AUTO_PREFIX + jobId), undefined);

/** Remove everything this app keeps in sessionStorage (used on a new session). */
export const clearAllSessionData = (): void =>
  safe(() => {
    const doomed: string[] = [];
    for (let i = 0; i < sessionStorage.length; i += 1) {
      const key = sessionStorage.key(i);
      if (key && key.startsWith("rw:")) doomed.push(key);
    }
    doomed.forEach((key) => sessionStorage.removeItem(key));
  }, undefined);

/** Older versions kept the active job in localStorage; drop it. */
export const removeLegacyLocalStorage = (): void =>
  safe(() => {
    const doomed: string[] = [];
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (key && key.startsWith("rw:")) doomed.push(key);
    }
    doomed.forEach((key) => localStorage.removeItem(key));
  }, undefined);
