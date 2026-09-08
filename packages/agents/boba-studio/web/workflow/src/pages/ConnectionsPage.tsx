import { Suspense, lazy, type ReactElement } from "react";

import { EmptyState } from "../ui";

// колонка каталога (версии снимков, sync) едет с чанком каталога
const Board = lazy(() =>
  import("../catalog/pages/CatalogConnections").then((module) => ({ default: module.CatalogConnections })),
);

/** Страница соединений studio: доска соединений, дополненная каталогом,
 * когда он включён и доступен. */
export function ConnectionsPage(): ReactElement {
  return (
    <Suspense fallback={<EmptyState fill title="loading connections" />}>
      <Board />
    </Suspense>
  );
}
