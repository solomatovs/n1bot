import { ChevronDown, Plus } from "lucide-react";
import { type ReactElement, useEffect, useRef, useState } from "react";

import type { ToolAvailability, ToolCatalog } from "../../model/workflow";

type Props = {
  catalog: ToolCatalog;
  onAdd: (tool: string) => void;
};

const ORDER: ToolAvailability[] = ["available", "chat_only", "denied"];

/** Меню «+ Tool» билдера: инструменты каталога по доступности. */
export function ToolMenu({ catalog, onAdd }: Props): ReactElement {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) {
      return;
    }

    const close = (event: MouseEvent): void => {
      if (root.current !== null && !root.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };

    document.addEventListener("mousedown", close);
    return () => {
      document.removeEventListener("mousedown", close);
    };
  }, [open]);

  const names = Object.keys(catalog).sort();
  return (
    <div className="menu" ref={root}>
      <button
        type="button"
        className="btn"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => {
          setOpen((current) => !current);
        }}
      >
        <Plus size={12} /> Tool <ChevronDown size={12} />
      </button>
      {open && (
        <div className="menu__list" role="menu" aria-label="tools">
          {ORDER.map((availability) => {
            const group = names.filter((name) => catalog[name]?.availability === availability);
            if (group.length === 0) {
              return null;
            }

            return (
              <div key={availability}>
                <div className="menu__group eyebrow">{availability.replace("_", " ")}</div>
                {group.map((name) => (
                  <button
                    type="button"
                    role="menuitem"
                    key={name}
                    className="menu__item"
                    disabled={availability !== "available"}
                    onClick={() => {
                      onAdd(name);
                      setOpen(false);
                    }}
                  >
                    {name}
                  </button>
                ))}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
