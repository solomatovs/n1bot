import { createContext, useContext, useMemo, type ReactElement } from "react";
import { BrowserRouter, Route, Routes, useParams } from "react-router-dom";

import { CatalogApi } from "./api/client";
import { PageUrls, pageConfig } from "./config";
import { CatalogPage, type PageSource } from "./pages/CatalogPage";
import { IndexPage } from "./pages/IndexPage";
import { EmptyState } from "./ui";

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

function ViewRoute(): ReactElement {
  const { viewId } = useParams();
  const source = useMemo<PageSource | null>(() => (viewId === undefined ? null : { kind: "view", viewId }), [viewId]);
  if (source === null) {
    return <EmptyState fill title="view id is missing" />;
  }

  return <CatalogPage source={source} />;
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

  return <CatalogPage source={source} />;
}

export function App(): ReactElement {
  const services = useMemo<Services>(() => {
    const urls = new PageUrls(pageConfig());
    return { urls, api: new CatalogApi(urls) };
  }, []);

  return (
    <ServicesContext.Provider value={services}>
      <BrowserRouter basename={services.urls.routerBase}>
        <Routes>
          <Route index element={<IndexPage />} />
          <Route path="/views/:viewId" element={<ViewRoute />} />
          <Route path="/drafts/:draftId" element={<DraftRoute />} />
          <Route path="*" element={<EmptyState fill title="no such page" />} />
        </Routes>
      </BrowserRouter>
    </ServicesContext.Provider>
  );
}
