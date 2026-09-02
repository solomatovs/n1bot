import { ChevronDown, Plus } from "lucide-react";
import { type ReactElement, useEffect, useRef, useState } from "react";

import type { ToolAvailability, ToolCatalog } from "../../model/workflow";
import { Button } from "../../ui";
import { Menu, MenuGroup, MenuItem, MenuList } from "../../ui";

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
    <Menu containerRef={root}>
      <Button
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => {
          setOpen((current) => !current);
        }}
      >
        <Plus size={12} /> Tool <ChevronDown size={12} />
      </Button>
      {open && (
        <MenuList label="tools">
          {ORDER.map((availability) => {
            const group = names.filter((name) => catalog[name]?.availability === availability);
            if (group.length === 0) {
              return null;
            }

            return (
              <div key={availability}>
                <MenuGroup>{availability.replace("_", " ")}</MenuGroup>
                {group.map((name) => (
                  <MenuItem
                    key={name}
                    disabled={availability !== "available"}
                    onClick={() => {
                      onAdd(name);
                      setOpen(false);
                    }}
                  >
                    {name}
                  </MenuItem>
                ))}
              </div>
            );
          })}
        </MenuList>
      )}
    </Menu>
  );
}
