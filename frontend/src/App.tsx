import { lazy, Suspense, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth/AuthContext";
import { Loading } from "./components/ui";
import { AppShell } from "./layout/AppShell";
import DomainGatePage from "./pages/auth/DomainGatePage";
import LoginPage from "./pages/auth/LoginPage";
import PasswordChangePage from "./pages/auth/PasswordChangePage";

const Overview = lazy(() => import("./pages/overview"));
const Readiness = lazy(() => import("./pages/readiness"));
const Decisions = lazy(() => import("./pages/decisions"));
const Projects = lazy(() => import("./pages/projects"));
const NewProject = lazy(() => import("./pages/projects/NewProjectPage"));
const ProjectDetail = lazy(() => import("./pages/projects/ProjectDetailPage"));
const Plans = lazy(() => import("./pages/plans"));
const PlanDetail = lazy(() => import("./pages/plans/PlanDetailPage"));
const Dag = lazy(() => import("./pages/dag"));
const Publications = lazy(() => import("./pages/publications"));
const Work = lazy(() => import("./pages/work"));
const RunDetail = lazy(() => import("./pages/work/RunDetailPage"));
const Organization = lazy(() => import("./pages/organization"));
const Evidence = lazy(() => import("./pages/evidence"));
const SystemMap = lazy(() => import("./pages/system-map"));
const Tools = lazy(() => import("./pages/tools"));
const ComponentDetail = lazy(() => import("./pages/tools/ComponentDetailPage"));
const Models = lazy(() => import("./pages/models"));
const Integrations = lazy(() => import("./pages/integrations"));
const Costs = lazy(() => import("./pages/costs"));
const Proposals = lazy(() => import("./pages/proposals"));
const ProposalDetail = lazy(() => import("./pages/proposals/ProposalDetailPage"));
const Research = lazy(() => import("./pages/research"));
const Evaluation = lazy(() => import("./pages/evaluation"));
const History = lazy(() => import("./pages/history"));
const Settings = lazy(() => import("./pages/settings"));

function S({ children }: { children: ReactNode }) {
  return <Suspense fallback={<Loading />}>{children}</Suspense>;
}

export default function App() {
  const { session, loading, domain } = useAuth();
  if (loading) return <Loading label="Memeriksa sesi…" />;
  if (!session) return <LoginPage />;
  if (session.user.must_change_password) return <PasswordChangePage forced />;
  if (!domain) return <DomainGatePage />;
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<S><Overview /></S>} />
        <Route path="readiness" element={<S><Readiness /></S>} />
        <Route path="decisions" element={<S><Decisions /></S>} />
        <Route path="projects" element={<S><Projects /></S>} />
        <Route path="projects/new" element={<S><NewProject /></S>} />
        <Route path="projects/:projectId" element={<S><ProjectDetail /></S>} />
        <Route path="plans" element={<S><Plans /></S>} />
        <Route path="plans/:planId" element={<S><PlanDetail /></S>} />
        <Route path="dag" element={<S><Dag /></S>} />
        <Route path="publications" element={<S><Publications /></S>} />
        <Route path="work" element={<S><Work /></S>} />
        <Route path="runs/:runId" element={<S><RunDetail /></S>} />
        <Route path="organization" element={<S><Organization /></S>} />
        <Route path="evidence" element={<S><Evidence /></S>} />
        <Route path="system-map" element={<S><SystemMap /></S>} />
        <Route path="tools" element={<S><Tools /></S>} />
        <Route path="tools/:componentId" element={<S><ComponentDetail /></S>} />
        <Route path="models" element={<S><Models /></S>} />
        <Route path="integrations" element={<S><Integrations /></S>} />
        <Route path="costs" element={<S><Costs /></S>} />
        <Route path="proposals" element={<S><Proposals /></S>} />
        <Route path="proposals/:proposalId" element={<S><ProposalDetail /></S>} />
        <Route path="research" element={<S><Research /></S>} />
        <Route path="evaluation" element={<S><Evaluation /></S>} />
        <Route path="history" element={<S><History /></S>} />
        <Route path="settings" element={<S><Settings /></S>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
