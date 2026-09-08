import { Suspense, useMemo, type ReactElement } from "react";
import { Outlet, useParams } from "react-router-dom";

import { useServices } from "../services";
import { EmptyState } from "../ui";
import { CatalogApi } from "./api/client";
import { ProcessPage, type PageSource } from "./pages/ProcessPage";
import { CatalogContext, type CatalogServices } from "./services";

/** Секция каталога под /catalog: клиент API каталога над общим транспортом
 * для вложенных страниц и заглушка на время загрузки их чанка. */
export default function CatalogSection(): ReactElement {
  const { api, transport } = useServices();
  const services = useMemo<CatalogServices>(() => ({ api: new CatalogApi(transport), workflowApi: api }), [api, transport]);

  return (
    <CatalogContext.Provider value={services}>
      <Suspense fallback={<EmptyState fill title="loading the catalog" />}>
        <Outlet />
      </Suspense>
    </CatalogContext.Provider>
  );
}

export function ProcessRoute(): ReactElement {
  const { processId } = useParams();
  const source = useMemo<PageSource | null>(() => {
    if (processId === undefined) {
      return null;
    }

    return { kind: "published", processId };
  }, [processId]);
  if (source === null) {
    return <EmptyState fill title="process id is missing" />;
  }

  return <ProcessPage source={source} />;
}

export function DraftRoute(): ReactElement {
  const { draftId } = useParams();
  const source = useMemo<PageSource | null>(() => {
    if (draftId === undefined) {
      return null;
    }

    return { kind: "draft", draftId };
  }, [draftId]);
  if (source === null) {
    return <EmptyState fill title="draft id is missing" />;
  }

  return <ProcessPage source={source} />;
}

export function SharedRoute(): ReactElement {
  const { token } = useParams();
  const source = useMemo<PageSource | null>(() => {
    if (token === undefined) {
      return null;
    }

    return { kind: "shared", token };
  }, [token]);
  if (source === null) {
    return <EmptyState fill title="share token is missing" />;
  }

  return <ProcessPage source={source} />;
}

export function NoSuchPage(): ReactElement {
  return <EmptyState fill title="no such page" />;
}
