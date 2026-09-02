import type { ReactElement } from "react";
import type { PropsWithChildren } from "react";

import { Alert } from "../ui/Alert";
import { EmptyState } from "../ui";

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
    return <EmptyState>Loading…</EmptyState>;
  }

  if (state.kind === "error") {
    return (
      <EmptyState>
        <Alert tone="error">{state.message}</Alert>
      </EmptyState>
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
