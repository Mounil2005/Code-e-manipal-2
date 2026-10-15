import { lazy, Suspense, useEffect } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { useSim } from "./store/sim";
import { useSimSocket } from "./hooks/useSimSocket";
import { Toasts, TopBar } from "./components/Shell";
import { DecisionInspector } from "./components/DecisionInspector";
import { Empty, Spinner } from "./components/ui";
import Command from "./pages/Command";

const Dispatch = lazy(() => import("./pages/Dispatch"));
const Judge = lazy(() => import("./pages/Judge"));
const Live = lazy(() => import("./pages/Live"));
const Data = lazy(() => import("./pages/Data"));
const Agents = lazy(() => import("./pages/Agents"));
const Scenarios = lazy(() => import("./pages/Scenarios"));
const DecisionLab = lazy(() => import("./pages/DecisionLab"));
const Arena = lazy(() => import("./pages/Arena"));
const Replay = lazy(() => import("./pages/Replay"));
const Safety = lazy(() => import("./pages/Safety"));
const Reports = lazy(() => import("./pages/Reports"));

export default function App() {
  const bootstrap = useSim((s) => s.bootstrap);
  const sim = useSim((s) => s.sim);
  const conn = useSim((s) => s.conn);
  useEffect(() => {
    bootstrap();
  }, [bootstrap]);
  useSimSocket(sim?.id);

  return (
    <div className="fog min-h-full">
      <TopBar />
      <main className="mx-auto max-w-[1680px] overflow-x-hidden">
        {conn === "offline" && !sim ? (
          <Empty title="Backend unreachable" body="Start the API with scripts/start-backend (port 8000), then reload this page." />
        ) : (
          <Suspense fallback={<Spinner label="Loading…" />}>
            <Routes>
              <Route path="/" element={<Navigate to="/command" replace />} />
              <Route path="/command" element={<Command />} />
              <Route path="/judge" element={<Judge />} />
              <Route path="/live" element={<Live />} />
              <Route path="/data" element={<Data />} />
              <Route path="/dispatch" element={<Dispatch />} />
              <Route path="/agents" element={<Agents />} />
              <Route path="/scenarios" element={<Scenarios />} />
              <Route path="/decision-lab" element={<DecisionLab />} />
              <Route path="/arena" element={<Arena />} />
              <Route path="/replay" element={<Replay />} />
              <Route path="/safety" element={<Safety />} />
              <Route path="/reports" element={<Reports />} />
              <Route path="*" element={<Navigate to="/command" replace />} />
            </Routes>
          </Suspense>
        )}
      </main>
      <DecisionInspector />
      <Toasts />
    </div>
  );
}
