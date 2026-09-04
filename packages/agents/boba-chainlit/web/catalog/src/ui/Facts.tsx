import type { ReactElement, ReactNode } from "react";

import "./Facts.css";

export type Fact = {
  label: ReactNode;
  value: ReactNode;
  /** Ключ для тестов: data-fact. */
  key?: string;
};

type Props = {
  facts: Fact[];
  /** Мелкая сетка значений внутри карточки потока. */
  micro?: boolean;
  mark?: string | undefined;
};

/** Пары «подпись — значение» сеткой в две колонки.
 * Единственное место, где существует класс `facts`. */
export function Facts({ facts, micro = false, mark }: Props): ReactElement {
  const classes = ["facts"];
  if (micro) {
    classes.push("facts--micro");
  }

  return (
    <dl className={classes.join(" ")} data-testid={mark}>
      {facts.map((fact, index) => (
        <FactRow key={fact.key ?? index} fact={fact} />
      ))}
    </dl>
  );
}

function FactRow({ fact }: { fact: Fact }): ReactElement {
  return (
    <>
      <dt>{fact.label}</dt>
      <dd data-fact={fact.key}>{fact.value}</dd>
    </>
  );
}
