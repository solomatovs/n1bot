import type { ReactElement } from "react";

import "./Segmented.css";

type Option<T extends string> = {
  value: T;
  label: string;
};

type Props<T extends string> = {
  options: Option<T>[];
  value: T;
  onChange: (value: T) => void;
  label: string;
  /** Во всю ширину контейнера: вкладки панели. */
  fill?: boolean;
};

/** Сегментированный переключатель в пилюле высотой в контрол: режимы
 * холста, вкладки панели. Единственное место с классом `segmented`. */
export function Segmented<T extends string>({ options, value, onChange, label, fill = false }: Props<T>): ReactElement {
  const classes = ["segmented"];
  if (fill) {
    classes.push("segmented--fill");
  }

  return (
    <div className={classes.join(" ")} role="tablist" aria-label={label}>
      {options.map((option) => (
        <button
          type="button"
          role="tab"
          aria-selected={option.value === value}
          key={option.value}
          className="segmented__item"
          onClick={() => {
            onChange(option.value);
          }}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
