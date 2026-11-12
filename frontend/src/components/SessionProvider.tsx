import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useNavigate } from "react-router-dom";
import {
  ApiError,
  SESSION_ENDED_EVENT,
  sendCloseNotice,
  sendHeartbeat,
} from "../api/client";
import { ensureUniqueSession, resetSession } from "../lib/session";
import { removeLegacyLocalStorage } from "../lib/storage";
import { Card, buttonClass } from "./Card";

interface SessionContextValue {
  /** Set when a previous session ended and a fresh one was started. */
  notice: string | null;
  dismissNotice: () => void;
}

const SessionContext = createContext<SessionContextValue>({
  notice: null,
  dismissNotice: () => undefined,
});

export const useSessionNotice = () => useContext(SessionContext);

const DEFAULT_HEARTBEAT_MS = 60_000;
const ENDED_NOTICE =
  "Your previous session ended and its files were removed. You're in a new session.";

/**
 * Keeps this tab's session alive and tells the backend when the page closes.
 * Children render only after the first heartbeat has succeeded, so a page
 * refresh (which also fires "pagehide") is recognised as the same session
 * before any job data is requested.
 */
export function SessionProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const [phase, setPhase] = useState<"starting" | "ready" | "error">("starting");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const intervalRef = useRef<number | undefined>(undefined);
  const heartbeatMs = useRef(DEFAULT_HEARTBEAT_MS);

  const startFreshSession = useCallback(async () => {
    resetSession();
    const result = await sendHeartbeat();
    heartbeatMs.current = result.heartbeat_seconds * 1000;
    setNotice(ENDED_NOTICE);
  }, []);

  useEffect(() => {
    let cancelled = false;
    removeLegacyLocalStorage();

    async function beat(): Promise<void> {
      try {
        const result = await sendHeartbeat();
        heartbeatMs.current = result.heartbeat_seconds * 1000;
      } catch (caught) {
        if (caught instanceof ApiError && caught.isGone) {
          await startFreshSession();
          navigate("/", { replace: true });
        }
        // other errors (offline, server restarting): try again next beat
      }
    }

    async function start() {
      try {
        await ensureUniqueSession();
        try {
          const result = await sendHeartbeat();
          heartbeatMs.current = result.heartbeat_seconds * 1000;
        } catch (caught) {
          if (caught instanceof ApiError && caught.isGone) {
            await startFreshSession();
          } else {
            throw caught;
          }
        }
        if (cancelled) return;
        setError(null);
        setPhase("ready");
        intervalRef.current = window.setInterval(() => void beat(), heartbeatMs.current);
      } catch (caught) {
        if (cancelled) return;
        setError(
          caught instanceof ApiError
            ? caught.message
            : "Couldn't start a session. Please try again.",
        );
        setPhase("error");
      }
    }

    const onPageHide = () => sendCloseNotice();
    const onPageShow = (event: PageTransitionEvent) => {
      if (event.persisted) void beat(); // restored from the back/forward cache
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") void beat();
    };
    const onSessionEnded = () => {
      void startFreshSession()
        .then(() => navigate("/", { replace: true }))
        .catch(() => undefined);
    };

    window.addEventListener("pagehide", onPageHide);
    window.addEventListener("pageshow", onPageShow);
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener(SESSION_ENDED_EVENT, onSessionEnded);
    void start();

    return () => {
      cancelled = true;
      window.clearInterval(intervalRef.current);
      window.removeEventListener("pagehide", onPageHide);
      window.removeEventListener("pageshow", onPageShow);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener(SESSION_ENDED_EVENT, onSessionEnded);
    };
  }, [attempt, navigate, startFreshSession]);

  if (phase === "starting") {
    return (
      <p className="p-8 text-center text-slate-400" role="status">
        Starting session…
      </p>
    );
  }

  if (phase === "error") {
    return (
      <div className="mx-auto max-w-md p-6">
        <Card className="text-center">
          <h1 className="text-lg font-semibold text-white">Can't reach the server</h1>
          <p className="mt-2 text-sm text-slate-400">{error}</p>
          <button
            className={`${buttonClass.primary} mt-4`}
            onClick={() => {
              setPhase("starting");
              setAttempt((n) => n + 1);
            }}
          >
            Try again
          </button>
        </Card>
      </div>
    );
  }

  return (
    <SessionContext.Provider value={{ notice, dismissNotice: () => setNotice(null) }}>
      {children}
    </SessionContext.Provider>
  );
}
