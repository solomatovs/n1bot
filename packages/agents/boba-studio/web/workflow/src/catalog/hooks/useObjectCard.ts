import { useCallback } from "react";

import type { Loadable } from "../../components/Async";
import { useLoadable } from "../../hooks/useLoadable";
import type { CatalogApi } from "../api/client";
import type { ObjectCard, ObjectRef } from "../model/catalog";
import { ObjectRefParam } from "../model/refParam";

/** Гость по ссылке: карточка узла отдаётся по токену, без доступа к подключению. */
export type SharedCardSource = {
  token: string;
  nodeId: string;
};

/** Родная карточка объекта снимка: перечитывается по смене подключения,
 * версии и адреса объекта. Адрес сравнивается строкой: узел приходит новым
 * объектом на каждую порцию черновика, и перечитывать по нему нельзя. */
export function useObjectCard(
  api: CatalogApi,
  connectionId: string,
  version: number,
  ref: ObjectRef,
  shared?: SharedCardSource,
): Loadable<ObjectCard> {
  const address = ObjectRefParam.render(ref);
  const token = shared?.token;
  const nodeId = shared?.nodeId;

  const load = useCallback(() => {
    if (token !== undefined && nodeId !== undefined) {
      return api.sharedObject(token, nodeId);
    }

    const parsed = ObjectRefParam.parse(address);
    if (parsed === undefined) {
      return Promise.reject(new Error(`node address is not readable: ${address}`));
    }

    return api.connectionObject(connectionId, version, parsed.kind, parsed.path);
  }, [api, connectionId, version, address, token, nodeId]);

  const [state] = useLoadable(load);
  return state;
}
