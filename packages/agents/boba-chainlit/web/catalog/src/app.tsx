import { createContext, useContext, useMemo, type ReactElement } from "react";
import { BrowserRouter, Route, Routes, useParams } from "react-router-dom";

import { CatalogApi } from "./api/client";
import { PageUrls, pageConfig } from "./config";
import { ConnectionPage } from "./pages/ConnectionPage";
import { ConnectionsPage } from "./pages/ConnectionsPage";
import { HomePage } from "./pages/HomePage";
import { ProcessPage, type PageSource } from "./pages/ProcessPage";
import { EmptyState, ToastProvider } from "./ui";

/** Общие для страниц службы: адреса и API-клиент. */
export type Services = {
  urls: PageUrls;
  api: CatalogApi;
};

const ServicesContext = createContext<Services | null>(null);

export function useServices(): Services {
  const services = useContext(ServicesContext);
  if (services === null) {
    throw new Error("services are provided by App only");
  }

  return services;
}

function ProcessRoute(): ReactElement {
  const { processId } = useParams();
  const source = useMemo<PageSource | null>(
    () => (processId === undefined ? null : { kind: "published", processId }),
    [processId],
  );
  if (source === null) {
    return <EmptyState fill title="process id is missing" />;
  }

  return <ProcessPage source={source} />;
}

function DraftRoute(): ReactElement {
  const { draftId } = useParams();
  const source = useMemo<PageSource | null>(
    () => (draftId === undefined ? null : { kind: "draft", draftId }),
    [draftId],
  );
  if (source === null) {
    return <EmptyState fill title="draft id is missing" />;
  }

  return <ProcessPage source={source} />;
}

function SharedRoute(): ReactElement {
  const { token } = useParams();
  const source = useMemo<PageSource | null>(() => (token === undefined ? null : { kind: "shared", token }), [token]);
  if (source === null) {
    return <EmptyState fill title="share token is missing" />;
  }

  return <ProcessPage source={source} />;
}

export function App(): ReactElement {
  const services = useMemo<Services>(() => {
    const urls = new PageUrls(pageConfig());
    return { urls, api: new CatalogApi(urls) };
  }, []);

  return (
    <ServicesContext.Provider value={services}>
      <ToastProvider>
        <BrowserRouter basename={services.urls.routerBase}>
          <Routes>
            <Route index element={<HomePage />} />
            <Route path="/processes/:processId" element={<ProcessRoute />} />
            <Route path="/drafts/:draftId" element={<DraftRoute />} />
            <Route path="/shared/:token" element={<SharedRoute />} />
            <Route path="/connections" element={<ConnectionsPage />} />
            <Route path="/connections/:connectionId" element={<ConnectionPage />} />
            <Route path="*" element={<EmptyState fill title="no such page" />} />
          </Routes>
        </BrowserRouter>
      </ToastProvider>
    </ServicesContext.Provider>
  );
}
