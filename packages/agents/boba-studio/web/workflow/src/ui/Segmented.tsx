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
};

/** Сегментированный переключатель в пилюле, как Grid/Table/Timeline у Studio. */
export function Segmented<T extends string>({ options, value, onChange, label }: Props<T>): ReactElement {
  return (
    <div className="segmented" role="tablist" aria-label={label}>
      {options.map((option) => (
        <button
          type="button"
          role="tab"
          aria-selected={option.value === value}
          key={option.value}
          className={`segmented__item${option.value === value ? " segmented__item--on" : ""}`}
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
