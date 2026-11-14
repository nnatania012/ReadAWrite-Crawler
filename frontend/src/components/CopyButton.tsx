import { useEffect, useRef, useState } from "react";
import { ApiError, getChapterContent } from "../api/client";

type Phase = "idle" | "copying" | "copied" | "error";

function describeApiError(error: ApiError): string {
  switch (error.code) {
    case "chapter_not_found":
      return "This chapter wasn't found.";
    case "chapter_unavailable":
      return error.message || "This chapter isn't available to copy.";
    case "job_expired":
    case "session_expired":
      return "This extraction's data has expired.";
    case "network_error":
      return "Can't reach the server. Check your connection and try again.";
    default:
      return error.message || "Couldn't get the chapter text.";
  }
}

function describeClipboardError(error: unknown): string {
  if (error instanceof DOMException && error.name === "NotAllowedError") {
    return "The browser blocked clipboard access. Select the text below and copy it manually.";
  }
  if (!window.isSecureContext) {
    return "Clipboard access needs a secure (HTTPS) page. Select the text below and copy it manually.";
  }
  return "Couldn't write to the clipboard. Select the text below and copy it manually.";
}

const Icon = ({ phase }: { phase: Phase }) => {
  if (phase === "copying") {
    return (
      <span
        aria-hidden
        className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent"
      />
    );
  }
  if (phase === "copied") {
    return (
      <svg aria-hidden viewBox="0 0 20 20" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 10.5l4 4 8-9" />
      </svg>
    );
  }
  return (
    <svg aria-hidden viewBox="0 0 20 20" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <rect x="7" y="7" width="9" height="10" rx="1.5" />
      <path d="M13 7V4.5A1.5 1.5 0 0 0 11.5 3h-6A1.5 1.5 0 0 0 4 4.5v8A1.5 1.5 0 0 0 5.5 14H7" />
    </svg>
  );
};

interface Props {
  jobId: string;
  chapterNumber: number;
  /** Backend-confirmed: this chapter is extracted and intact (the job may still be running). */
  enabled: boolean;
  /** Shown as "Unavailable" instead of "Copy" (chapter can never be copied). */
  unavailable: boolean;
  /** Why the button is disabled (tooltip). */
  disabledReason: string;
}

/**
 * Per-chapter copy control. The text is fetched only when clicked, goes
 * straight from the response into navigator.clipboard.writeText, and is not
 * kept afterwards. Each button has its own state, so copying one chapter
 * never blocks another.
 */
export function CopyButton({
  jobId,
  chapterNumber,
  enabled,
  unavailable,
  disabledReason,
}: Props) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [message, setMessage] = useState<string | null>(null);
  // Only used when the clipboard can't be written, so the person can still
  // select and copy the text by hand. Cleared as soon as it is dismissed.
  const [fallbackText, setFallbackText] = useState<string | null>(null);
  const inFlight = useRef(false);
  const timer = useRef<number | undefined>(undefined);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  async function onClick() {
    if (inFlight.current || !enabled) return; // no duplicate requests
    inFlight.current = true;
    window.clearTimeout(timer.current);
    setPhase("copying");
    setMessage(null);
    setFallbackText(null);

    let text: string;
    try {
      text = await getChapterContent(jobId, chapterNumber);
    } catch (caught) {
      setPhase("error");
      setMessage(
        caught instanceof ApiError
          ? describeApiError(caught)
          : "Something went wrong while getting the chapter.",
      );
      inFlight.current = false;
      return;
    }

    try {
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
        throw new Error("clipboard-unavailable");
      }
      await navigator.clipboard.writeText(text); // the exact string received
      setPhase("copied");
      setMessage(null);
      timer.current = window.setTimeout(() => setPhase("idle"), 2000);
    } catch (caught) {
      setPhase("error");
      setMessage(describeClipboardError(caught));
      setFallbackText(text); // kept only so it can be selected manually
    } finally {
      inFlight.current = false;
    }
  }

  const disabled = !enabled || phase === "copying";
  const label = unavailable
    ? "Unavailable"
    : phase === "copying"
      ? "Copying…"
      : phase === "copied"
        ? "Copied!"
        : "Copy";

  const tone =
    phase === "copied"
      ? "border-emerald-700 bg-emerald-950 text-emerald-300"
      : "border-slate-700 bg-slate-800 text-slate-200 hover:bg-slate-700 hover:text-white";

  return (
    <div className="ml-auto flex flex-col items-end">
      <button
        type="button"
        onClick={() => void onClick()}
        disabled={disabled}
        aria-label={
          unavailable
            ? `Chapter ${chapterNumber} can't be copied`
            : `Copy chapter ${chapterNumber}`
        }
        title={!enabled ? disabledReason : "Copy this chapter's text"}
        className={`inline-flex min-w-[5.75rem] items-center justify-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:cursor-not-allowed disabled:border-slate-800 disabled:bg-slate-900 disabled:text-slate-600 ${tone}`}
      >
        <Icon phase={phase} />
        {label}
      </button>

      <span role="status" aria-live="polite" className="sr-only">
        {phase === "copied" ? `Chapter ${chapterNumber} copied to the clipboard` : ""}
      </span>

      {message && (
        <p role="alert" className="mt-1 max-w-xs text-right text-xs text-rose-300">
          {message}
        </p>
      )}

      {fallbackText !== null && (
        <div className="mt-2 w-[min(32rem,80vw)] rounded-lg border border-slate-700 bg-slate-950 p-2">
          <textarea
            ref={textareaRef}
            readOnly
            value={fallbackText}
            aria-label={`Text of chapter ${chapterNumber}`}
            onFocus={(event) => event.currentTarget.select()}
            className="h-40 w-full resize-y rounded bg-slate-900 p-2 font-mono text-xs text-slate-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"
          />
          <div className="mt-2 flex justify-end gap-2">
            <button
              type="button"
              className="rounded px-2 py-1 text-xs text-indigo-300 hover:bg-slate-800"
              onClick={() => textareaRef.current?.select()}
            >
              Select all
            </button>
            <button
              type="button"
              className="rounded px-2 py-1 text-xs text-slate-400 hover:bg-slate-800"
              onClick={() => {
                setFallbackText(null);
                setMessage(null);
                setPhase("idle");
              }}
            >
              Close
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
