import { createContext, lazy, useContext, useEffect, type ReactElement } from "react";
import { Route } from "react-router-dom";

import { useServices } from "../services";
import type { WorkflowApi } from "../api/client";
import { PageUrls } from "../config";
import type { CatalogChanged } from "../model/workflow";
import type { CatalogApi } from "./api/client";

/** Службы страниц каталога: API каталога и общий API studio (соединения брокера). */
export type CatalogServices = {
  api: CatalogApi;
  workflowApi: WorkflowApi;
};

export const CatalogContext = createContext<CatalogServices | null>(null);

export function useCatalog(): CatalogServices {
  const services = useContext(CatalogContext);
  if (services === null) {
    throw new Error("catalog services are provided by the catalog section only");
  }

  return services;
}

export type CatalogChangeListener = (message: CatalogChanged) => void;

/** Живые изменения каталога из ленты пользователя (socket.io user_event):
 * подписка на время жизни слушателя; null — не слушать (гость по ссылке). */
export function useCatalogChanges(listener: CatalogChangeListener | null): void {
  const { socket } = useServices();

  useEffect(() => {
    if (listener === null) {
      return undefined;
    }

    return socket.onUser((event) => {
      if (event.kind !== "catalog_changed") {
        return;
      }

      listener(event);
    });
  }, [socket, listener]);
}

// код каталога (с ELK) грузится отдельным чанком при первом заходе под /catalog
const CatalogSection = lazy(() => import("./section"));
const HomePage = lazy(() => import("./pages/HomePage").then((module) => ({ default: module.HomePage })));
const ConnectionsPage = lazy(() =>
  import("./pages/ConnectionsPage").then((module) => ({ default: module.ConnectionsPage })),
);
const ConnectionPage = lazy(() =>
  import("./pages/ConnectionPage").then((module) => ({ default: module.ConnectionPage })),
);
const ProcessRoute = lazy(() => import("./section").then((module) => ({ default: module.ProcessRoute })));
const DraftRoute = lazy(() => import("./section").then((module) => ({ default: module.DraftRoute })));
const SharedRoute = lazy(() => import("./section").then((module) => ({ default: module.SharedRoute })));
const NoSuchPage = lazy(() => import("./section").then((module) => ({ default: module.NoSuchPage })));

/** Страницы каталога: маршруты под /catalog для Routes приложения; адреса —
 * из PageUrls, 401 ловит общий транспорт, службы каталога даёт секция. */
export function catalogRoutes(): ReactElement {
  return (
    <Route element={<CatalogSection />}>
      <Route path={PageUrls.catalog.home()} element={<HomePage />} />
      <Route path={PageUrls.catalog.process(":processId")} element={<ProcessRoute />} />
      <Route path={PageUrls.catalog.draft(":draftId")} element={<DraftRoute />} />
      <Route path={PageUrls.catalog.shared(":token")} element={<SharedRoute />} />
      <Route path={PageUrls.catalog.connections()} element={<ConnectionsPage />} />
      <Route path={PageUrls.catalog.connection(":connectionId")} element={<ConnectionPage />} />
      <Route path={`${PageUrls.catalog.home()}/*`} element={<NoSuchPage />} />
    </Route>
  );
}
