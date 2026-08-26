import { type ReactElement, createContext, useContext, useMemo } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { WorkflowApi } from "./api/client";
import { Layout } from "./components/Layout";
import { PageUrls, pageConfig } from "./config";
import { EditorPage } from "./pages/EditorPage";
import { ListPage } from "./pages/ListPage";
import { RunPage } from "./pages/RunPage";

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

export function App(): ReactElement {
  const services = useMemo<Services>(() => {
    const urls = new PageUrls(pageConfig());
    return { urls, api: new WorkflowApi(urls) };
  }, []);

  return (
    <ServicesContext.Provider value={services}>
      <BrowserRouter basename={services.urls.routerBase}>
        <Layout>
          <Routes>
            <Route path="/" element={<ListPage />} />
            <Route path="/new" element={<EditorPage />} />
            <Route path="/w/:workflowId" element={<EditorPage />} />
            <Route path="/run/:runId" element={<RunPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Layout>
      </BrowserRouter>
    </ServicesContext.Provider>
  );
}
