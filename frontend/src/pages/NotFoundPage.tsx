import { Link } from "react-router-dom";
import { Card, buttonClass } from "../components/Card";

export function NotFoundPage() {
  return (
    <Card className="mx-auto max-w-xl text-center">
      <h1 className="text-xl font-semibold text-white">Page not found</h1>
      <p className="mt-2 text-slate-400">That page doesn't exist.</p>
      <Link to="/" className={`${buttonClass.primary} mt-5`}>Go home</Link>
    </Card>
  );
}
