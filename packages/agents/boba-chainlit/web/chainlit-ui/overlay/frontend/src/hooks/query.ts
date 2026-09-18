import { useMemo } from 'react';
import { useInRouterContext, useLocation } from 'react-router-dom';

const NO_SEARCH = '';

function useRouterSearch(): string {
  const { search } = useLocation();
  return search;
}

// copilot рендерит композер без роутера: там строки запроса нет, и useLocation бросил бы
export function useQuery() {
  const inRouter = useInRouterContext();
  const search = inRouter ? useRouterSearch() : NO_SEARCH;

  return useMemo(() => new URLSearchParams(search), [search]);
}
