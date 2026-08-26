import { createContext, type ReactElement, useContext, useMemo } from "react";
import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router-dom";

import { WorkflowApi } from "./api/client";
import { Shell } from "./components/shell/Shell";
import { PageUrls, pageConfig } from "./config";
import { BuildPage } from "./pages/BuildPage";
import { ObservePage } from "./pages/ObservePage";

/** Общие для страниц службы: адреса и API-клиент. */
export type Services = {
  urls: PageUrls;
  api: WorkflowApi;
};

const ServicesContext = createContext<Services | null>(null);

export function useServices(): Services {
  const services = useContext(ServicesContext);
  if (services === null) {
    throw new Error("services are provided by App only");
  }

  return services;
}

function LegacyRun(): ReactElement {
  const { runId } = useParams();
  return <Navigate to={`/observe/${runId ?? ""}`} replace />;
}

function LegacyWorkflow(): ReactElement {
  const { workflowId } = useParams();
  return <Navigate to={`/build/${workflowId ?? ""}`} replace />;
}

export function App(): ReactElement {
  const services = useMemo<Services>(() => {
    const urls = new PageUrls(pageConfig());
    return { urls, api: new WorkflowApi(urls) };
  }, []);

  return (
    <ServicesContext.Provider value={services}>
      <BrowserRouter basename={services.urls.routerBase}>
        <Routes>
          <Route element={<Shell mode="observe" />}>
            <Route path="/observe" element={<ObservePage />} />
            <Route path="/observe/:runId" element={<ObservePage />} />
          </Route>
          <Route element={<Shell mode="build" />}>
            <Route path="/build" element={<BuildPage />} />
            <Route path="/build/new" element={<BuildPage />} />
            <Route path="/build/:workflowId" element={<BuildPage />} />
          </Route>
          <Route path="/run/:runId" element={<LegacyRun />} />
          <Route path="/w/:workflowId" element={<LegacyWorkflow />} />
          <Route path="/new" element={<Navigate to="/build/new" replace />} />
          <Route path="*" element={<Navigate to="/observe" replace />} />
        </Routes>
      </BrowserRouter>
    </ServicesContext.Provider>
  );
}
