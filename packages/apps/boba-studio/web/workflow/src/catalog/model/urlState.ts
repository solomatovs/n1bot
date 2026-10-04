import type { ObjectRef } from "./catalog";
import { isShowMode, type ShowMode } from "./graph";
import { ObjectRefParam } from "./refParam";
import { ConnectionUrlKey, DIFF_MODE, UrlKey, type PaneTab } from "./urlKeys";

export type { PaneTab } from "./urlKeys";

/** Состояние страницы в адресе: активный узел, выбранный объект источника,
 * режим карточек, скрытые узлы, показ diff, вкладка панели. Ссылку можно
 * передать: другой человек увидит то же самое. */
export type UrlState = {
  active: string | undefined;
  object: ObjectRef | undefined;
  showMode: ShowMode;
  hidden: ReadonlySet<string>;
  showDiff: boolean;
  pane: PaneTab;
};

export function readUrlState(params: URLSearchParams): UrlState {
  const mode = params.get(UrlKey.showMode) ?? "";
  const hiddenRaw = params.get(UrlKey.hidden) ?? "";
  const hidden = new Set(hiddenRaw.split(",").filter((id) => id !== ""));

  return {
    active: params.get(UrlKey.active) ?? undefined,
    object: ObjectRefParam.parse(params.get(UrlKey.object)),
    showMode: isShowMode(mode) ? mode : "KEY_ONLY",
    hidden,
    showDiff: params.get(UrlKey.showDiff) !== "0",
    pane: params.get(UrlKey.pane) === "connections" ? "connections" : "process",
  };
}

export function writeUrlState(state: UrlState, params: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(params);
  if (state.active === undefined) {
    next.delete(UrlKey.active);
  } else {
    next.set(UrlKey.active, state.active);
  }

  if (state.object === undefined) {
    next.delete(UrlKey.object);
  } else {
    next.set(UrlKey.object, ObjectRefParam.render(state.object));
  }

  if (state.showMode === "KEY_ONLY") {
    next.delete(UrlKey.showMode);
  } else {
    next.set(UrlKey.showMode, state.showMode);
  }

  if (state.hidden.size === 0) {
    next.delete(UrlKey.hidden);
  } else {
    next.set(UrlKey.hidden, [...state.hidden].sort().join(","));
  }

  if (state.showDiff) {
    next.delete(UrlKey.showDiff);
  } else {
    next.set(UrlKey.showDiff, "0");
  }

  // вкладка всегда в адресе: у обеих вкладок явная ссылка
  next.set(UrlKey.pane, state.pane);

  return next;
}

/** Состояние страницы подключения в адресе: запрошенная версия снимка
 * (-1 — последняя), показ diff с предыдущей и выбранный объект дерева. */
export type ConnectionUrlState = {
  version: number;
  showDiff: boolean;
  ref: ObjectRef | undefined;
};

export function readConnectionUrlState(params: URLSearchParams): ConnectionUrlState {
  return {
    version: Number(params.get(ConnectionUrlKey.version) ?? "-1"),
    showDiff: params.get(ConnectionUrlKey.mode) === DIFF_MODE,
    ref: ObjectRefParam.parse(params.get(ConnectionUrlKey.ref)),
  };
}

export function writeConnectionUrlState(state: ConnectionUrlState, params: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(params);
  if (state.version < 0) {
    next.delete(ConnectionUrlKey.version);
  } else {
    next.set(ConnectionUrlKey.version, String(state.version));
  }

  if (state.showDiff) {
    next.set(ConnectionUrlKey.mode, DIFF_MODE);
  } else {
    next.delete(ConnectionUrlKey.mode);
  }

  if (state.ref === undefined) {
    next.delete(ConnectionUrlKey.ref);
  } else {
    next.set(ConnectionUrlKey.ref, ObjectRefParam.render(state.ref));
  }

  return next;
}
