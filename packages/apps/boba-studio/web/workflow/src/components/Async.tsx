import type { ReactElement } from "react";

import { Alert } from "../ui/Alert";
import { EmptyState } from "../ui";

export type Loadable<T> =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; value: T };

type Title = {
  loading: string;
  failed: string;
};

type Props<T> = {
  state: Loadable<T>;
  render: (value: T) => ReactElement;
  /** Заглушки на всю сцену. */
  fill?: boolean;
  /** Заголовки заглушек: без них — общий «Loading…» и ошибка полосой. */
  title?: Title;
};

/** Три состояния загрузки одним местом: заглушка ожидания, ошибка, содержимое. */
export function Async<T>({ state, render, fill = false, title }: Props<T>): ReactElement {
  if (state.kind === "loading") {
    if (title === undefined) {
      return <EmptyState fill={fill}>Loading…</EmptyState>;
    }

    return <EmptyState fill={fill} title={title.loading} />;
  }

  if (state.kind === "error") {
    if (title === undefined) {
      return (
        <EmptyState fill={fill}>
          <Alert tone="error">{state.message}</Alert>
        </EmptyState>
      );
    }

    return (
      <EmptyState fill={fill} title={title.failed}>
        {state.message}
      </EmptyState>
    );
  }

  return render(state.value);
}
