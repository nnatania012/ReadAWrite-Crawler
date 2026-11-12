import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { SessionProvider } from "./components/SessionProvider";
import { HistoryPage } from "./pages/HistoryPage";
import { HomePage } from "./pages/HomePage";
import { JobPage } from "./pages/JobPage";
import { NotFoundPage } from "./pages/NotFoundPage";

export default function App() {
  return (
    <SessionProvider>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<HomePage />} />
          <Route path="jobs" element={<HistoryPage />} />
          <Route path="jobs/:jobId" element={<JobPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
    </SessionProvider>
  );
}
