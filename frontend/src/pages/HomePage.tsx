import { FormEvent, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, createJob } from "../api/client";
import { ActiveJobBanner } from "../components/ActiveJobBanner";
import { Card, Notice, buttonClass } from "../components/Card";
import { SessionNotice } from "../components/SessionNotice";
import { addSessionJobId } from "../lib/session";
import { setActiveJobId } from "../lib/storage";
import { validateOutputName, validateUrl } from "../lib/validation";

export function HomePage() {
  const navigate = useNavigate();
  const [url, setUrl] = useState("");
  const [outputName, setOutputName] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [failure, setFailure] = useState<ApiError | null>(null);
  const inFlight = useRef(false); // blocks double submits before state updates

  const urlError = submitted ? validateUrl(url) : null;
  const nameError = submitted ? validateOutputName(outputName) : null;

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (inFlight.current) return;
    setSubmitted(true);
    setFailure(null);
    if (validateUrl(url) || validateOutputName(outputName)) return;

    inFlight.current = true;
    setSubmitting(true);
    try {
      const accepted = await createJob({
        url: url.trim(),
        output_name: outputName.trim() || null,
      });
      setActiveJobId(accepted.job_id);
      addSessionJobId(accepted.job_id);
      navigate(`/jobs/${accepted.job_id}`);
    } catch (caught) {
      setFailure(
        caught instanceof ApiError
          ? caught
          : new ApiError("Something went wrong. Please try again.", {
              status: 0,
              code: "unknown",
              kind: "network",
            }),
      );
      inFlight.current = false;
      setSubmitting(false);
    }
  }

  const existingJob =
    failure?.code === "job_conflict" && typeof failure.extra.job_id === "string"
      ? failure.extra.job_id
      : null;

  return (
    <div className="mx-auto max-w-2xl space-y-5">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-white sm:text-3xl">
          Extract a story
        </h1>
        <p className="mt-2 text-slate-400">
          Paste a ReadAWrite story page. Every chapter is extracted on the server and
          bundled into a ZIP of numbered text files.
        </p>
      </div>

      <SessionNotice />
      <ActiveJobBanner />

      <Card>
        <form onSubmit={onSubmit} noValidate className="space-y-5">
          <div>
            <label htmlFor="url" className="mb-1.5 block text-sm font-medium text-slate-200">
              Story URL
            </label>
            <input
              id="url"
              name="url"
              type="url"
              inputMode="url"
              autoComplete="off"
              spellCheck={false}
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://www.readawrite.com/a/0123456789abcdef"
              aria-invalid={urlError ? true : undefined}
              aria-describedby={urlError ? "url-error" : "url-hint"}
              disabled={submitting}
              className={`w-full rounded-xl border bg-slate-950 px-4 py-3 text-sm text-white placeholder:text-slate-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:opacity-60 ${
                urlError ? "border-rose-600" : "border-slate-700"
              }`}
            />
            {urlError ? (
              <p id="url-error" role="alert" className="mt-1.5 text-sm text-rose-300">
                {urlError}
              </p>
            ) : (
              <p id="url-hint" className="mt-1.5 text-xs text-slate-500">
                The page that lists the story's chapters (readawrite.com, https).
              </p>
            )}
          </div>

          <div>
            <label htmlFor="output" className="mb-1.5 block text-sm font-medium text-slate-200">
              Output folder name <span className="font-normal text-slate-500">(optional)</span>
            </label>
            <input
              id="output"
              name="output"
              type="text"
              autoComplete="off"
              maxLength={100}
              value={outputName}
              onChange={(e) => setOutputName(e.target.value)}
              placeholder="My Story"
              aria-invalid={nameError ? true : undefined}
              aria-describedby={nameError ? "output-error" : "output-hint"}
              disabled={submitting}
              className={`w-full rounded-xl border bg-slate-950 px-4 py-3 text-sm text-white placeholder:text-slate-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:opacity-60 ${
                nameError ? "border-rose-600" : "border-slate-700"
              }`}
            />
            {nameError ? (
              <p id="output-error" role="alert" className="mt-1.5 text-sm text-rose-300">
                {nameError}
              </p>
            ) : (
              <p id="output-hint" className="mt-1.5 text-xs text-slate-500">
                Names the ZIP and its folder. Leave empty to use a default.
              </p>
            )}
          </div>

          {failure && (
            <Notice tone="danger" title="Couldn't start the extraction">
              <p>{failure.message}</p>
              {existingJob && (
                <p className="mt-1">
                  <Link className="underline" to={`/jobs/${existingJob}`}>
                    Open the existing job
                  </Link>
                </p>
              )}
            </Notice>
          )}

          <button type="submit" disabled={submitting} className={`${buttonClass.primary} w-full sm:w-auto`}>
            {submitting ? (
              <>
                <span
                  aria-hidden
                  className="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white"
                />
                Starting…
              </>
            ) : (
              "Start Extraction"
            )}
          </button>
        </form>
      </Card>
    </div>
  );
}
