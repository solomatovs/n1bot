import { Search as SearchIcon } from "lucide-react";
import type { ReactElement } from "react";

import { controlClasses } from "./Input";
import "./Search.css";

type Props = {
  value: string;
  onChange: (value: string) => void;
  label: string;
  placeholder?: string;
};

/** Поле поиска с иконкой во всю ширину контейнера.
 * Единственное место, где существует класс `search`. */
export function Search({ value, onChange, label, placeholder }: Props): ReactElement {
  return (
    <div className="search">
      <span className="search__icon">
        <SearchIcon size={12} />
      </span>
      <input
        type="search"
        className={controlClasses({ mono: true }, "input--search")}
        aria-label={label}
        placeholder={placeholder}
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      />
    </div>
  );
}
