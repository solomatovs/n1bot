import type { ReactElement } from "react";

import type { ToolAvailability, ToolCatalog } from "../../model/workflow";

type Props = {
  catalog: ToolCatalog;
  onAdd: (tool: string) => void;
};

const ORDER: ToolAvailability[] = ["available", "chat_only", "denied"];

/** Палитра инструментов из каталога субъекта; недоступные видны, но серые. */
export function Palette({ catalog, onAdd }: Props): ReactElement {
  const names = Object.keys(catalog).sort();
  return (
    <aside className="palette">
      <div className="palette__title">Tools</div>
      {ORDER.map((availability) => {
        const group = names.filter((name) => catalog[name]?.availability === availability);
        if (group.length === 0) {
          return null;
        }

        return (
          <div className="palette__group" key={availability}>
            <div className="palette__group-title">{availability.replace("_", " ")}</div>
            {group.map((name) => (
              <button
                type="button"
                key={name}
                className="palette__tool mono"
                disabled={availability !== "available"}
                onClick={() => {
                  onAdd(name);
                }}
                title={availability === "available" ? `add ${name}` : `${name}: ${availability}`}
              >
                {name}
              </button>
            ))}
          </div>
        );
      })}
    </aside>
  );
}
