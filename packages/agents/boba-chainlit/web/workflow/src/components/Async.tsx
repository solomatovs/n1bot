import type { ReactElement } from "react";
import type { PropsWithChildren } from "react";

export type Loadable<T> =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; value: T };

type Props<T> = PropsWithChildren<{
  state: Loadable<T>;
  render: (value: T) => ReactElement;
}>;

/** Три состояния загрузки одним местом: спиннер, ошибка, содержимое. */
export function Async<T>({ state, render }: Props<T>): ReactElement {
  if (state.kind === "loading") {
    return <div className="empty">Loading…</div>;
  }

  if (state.kind === "error") {
    return (
      <div className="empty">
        <span className="notice notice--error">{state.message}</span>
      </div>
    );
  }

  return render(state.value);
}

export function errorText(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }

  return String(error);
}
