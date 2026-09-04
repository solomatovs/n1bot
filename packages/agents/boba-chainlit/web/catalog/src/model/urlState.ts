import type { ObjectRef } from "./catalog";
import { isShowMode, type ShowMode } from "./graph";
import { ObjectParam } from "./refParam";

/** Вкладка левой панели: узлы процесса по слоям или деревья источников. */
export type PaneTab = "process" | "sources";

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

const KEY = {
  active: "active",
  object: "object",
  showMode: "mode",
  hidden: "hidden",
  showDiff: "diff",
  pane: "pane",
} as const;

export function readUrlState(params: URLSearchParams): UrlState {
  const mode = params.get(KEY.showMode) ?? "";
  const hiddenRaw = params.get(KEY.hidden) ?? "";
  const hidden = new Set(hiddenRaw.split(",").filter((id) => id !== ""));

  return {
    active: params.get(KEY.active) ?? undefined,
    object: ObjectParam.parse(params.get(KEY.object)),
    showMode: isShowMode(mode) ? mode : "KEY_ONLY",
    hidden,
    showDiff: params.get(KEY.showDiff) !== "0",
    pane: params.get(KEY.pane) === "sources" ? "sources" : "process",
  };
}

export function writeUrlState(state: UrlState, params: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(params);
  if (state.active === undefined) {
    next.delete(KEY.active);
  } else {
    next.set(KEY.active, state.active);
  }

  if (state.object === undefined) {
    next.delete(KEY.object);
  } else {
    next.set(KEY.object, ObjectParam.render(state.object));
  }

  if (state.showMode === "KEY_ONLY") {
    next.delete(KEY.showMode);
  } else {
    next.set(KEY.showMode, state.showMode);
  }

  if (state.hidden.size === 0) {
    next.delete(KEY.hidden);
  } else {
    next.set(KEY.hidden, [...state.hidden].sort().join(","));
  }

  if (state.showDiff) {
    next.delete(KEY.showDiff);
  } else {
    next.set(KEY.showDiff, "0");
  }

  if (state.pane === "process") {
    next.delete(KEY.pane);
  } else {
    next.set(KEY.pane, state.pane);
  }

  return next;
}
