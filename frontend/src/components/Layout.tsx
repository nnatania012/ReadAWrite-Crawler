import { NavLink, Outlet } from "react-router-dom";

const link = ({ isActive }: { isActive: boolean }) =>
  `rounded-lg px-3 py-1.5 text-sm font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 ${
    isActive ? "bg-slate-800 text-white" : "text-slate-400 hover:text-white"
  }`;

export function Layout() {
  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-800 bg-slate-950/80 backdrop-blur">
        <div className="mx-auto flex max-w-5xl items-center justify-between gap-4 px-4 py-3 sm:px-6">
          <NavLink
            to="/"
            className="flex items-center gap-2 rounded-lg text-base font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"
          >
            <span aria-hidden className="grid h-7 w-7 place-items-center rounded-lg bg-indigo-500 text-sm">
              R
            </span>
            ReadAWrite Extractor
          </NavLink>
          <nav aria-label="Main" className="flex gap-1">
            <NavLink to="/" end className={link}>
              Extract
            </NavLink>
            <NavLink to="/jobs" end className={link}>
              History
            </NavLink>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-5xl px-4 py-8 sm:px-6">
        <Outlet />
      </main>
    </div>
  );
}
