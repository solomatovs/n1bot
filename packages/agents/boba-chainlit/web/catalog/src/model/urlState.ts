import { isShowMode, type ShowMode } from "./graph";

/** Состояние страницы в адресе: активный набор, режим карточек, скрытые наборы,
 * показ diff. Ссылку можно передать: другой человек увидит то же самое. */
export type UrlState = {
  active: string | undefined;
  showMode: ShowMode;
  hidden: ReadonlySet<string>;
  showDiff: boolean;
};

const KEY = {
  active: "active",
  showMode: "mode",
  hidden: "hidden",
  showDiff: "diff",
} as const;

export function readUrlState(params: URLSearchParams): UrlState {
  const mode = params.get(KEY.showMode) ?? "";
  const hiddenRaw = params.get(KEY.hidden) ?? "";
  const hidden = new Set(hiddenRaw.split(",").filter((id) => id !== ""));

  return {
    active: params.get(KEY.active) ?? undefined,
    showMode: isShowMode(mode) ? mode : "KEY_ONLY",
    hidden,
    showDiff: params.get(KEY.showDiff) !== "0",
  };
}

export function writeUrlState(state: UrlState, params: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(params);
  if (state.active === undefined) {
    next.delete(KEY.active);
  } else {
    next.set(KEY.active, state.active);
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

  return next;
}
