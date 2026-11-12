import type { ReactNode } from "react";

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-2xl border border-slate-800 bg-slate-900/70 p-5 shadow-lg shadow-black/20 sm:p-6 ${className}`}
    >
      {children}
    </section>
  );
}

export function Notice({
  tone,
  title,
  children,
}: {
  tone: "info" | "success" | "warning" | "danger";
  title: string;
  children?: ReactNode;
}) {
  const classes = {
    info: "border-sky-800 bg-sky-950/60 text-sky-200",
    success: "border-emerald-800 bg-emerald-950/60 text-emerald-200",
    warning: "border-amber-800 bg-amber-950/60 text-amber-200",
    danger: "border-rose-800 bg-rose-950/60 text-rose-200",
  }[tone];
  return (
    <div
      role={tone === "danger" || tone === "warning" ? "alert" : "status"}
      className={`rounded-xl border px-4 py-3 text-sm ${classes}`}
    >
      <p className="font-semibold">{title}</p>
      {children && <div className="mt-1 opacity-90">{children}</div>}
    </div>
  );
}

const BTN =
  "inline-flex items-center justify-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950 disabled:cursor-not-allowed disabled:opacity-50";

export const buttonClass = {
  primary: `${BTN} bg-indigo-500 text-white hover:bg-indigo-400`,
  success: `${BTN} bg-emerald-500 text-emerald-950 hover:bg-emerald-400`,
  secondary: `${BTN} border border-slate-700 bg-slate-800 text-slate-100 hover:bg-slate-700`,
  danger: `${BTN} border border-rose-800 bg-rose-950 text-rose-200 hover:bg-rose-900`,
};
