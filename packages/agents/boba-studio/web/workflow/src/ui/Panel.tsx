import { type CSSProperties, type PointerEvent as ReactPointerEvent, type ReactElement, type ReactNode, useCallback, useState } from "react";

import "./Panel.css";

type Props = {
  "aria-label": string;
  className?: string | undefined;
  /** Узкий экран: панель — ящик поверх сцены, видимость решает open. */
  open: boolean;
  /** Широкий экран: свёрнутая панель скрыта, сцена забирает место. */
  collapsed: boolean;
  narrow: boolean;
  /** Ключ localStorage: панель помнит ширину, выбранную ресайзом. */
  storageKey: string;
  min?: number;
  max?: number;
  children: ReactNode;
};

function storedWidth(key: string, min: number, max: number): number | null {
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === null) {
      return null;
    }

    const parsed = Number(raw);
    if (Number.isFinite(parsed)) {
      return Math.min(Math.max(parsed, min), max);
    }
  } catch {
    // приватное окно: настройка живёт до перезагрузки
  }

  return null;
}

/** Боковая панель: ресайз за правый край с памятью ширины, сворачивание на
 * широком экране, ящик на узком. Единственное место с классом `list__resize`. */
export function Panel({
  className,
  open,
  collapsed,
  narrow,
  storageKey,
  min = 200,
  max = 560,
  children,
  ...rest
}: Props): ReactElement {
  const [width, setWidth] = useState<number | null>(() => storedWidth(storageKey, min, max));

  const resizeStart = useCallback(
    (event: ReactPointerEvent<HTMLElement>) => {
      event.preventDefault();
      const startX = event.clientX;
      const panel = (event.target as HTMLElement).closest("aside");
      const startWidth = panel === null ? min : panel.getBoundingClientRect().width;

      const clamp = (x: number): number => Math.min(Math.max(startWidth + x - startX, min), max);
      const onMove = (move: PointerEvent): void => {
        setWidth(clamp(move.clientX));
      };
      const onUp = (up: PointerEvent): void => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        try {
          window.localStorage.setItem(storageKey, String(Math.round(clamp(up.clientX))));
        } catch {
          // приватное окно: настройка живёт до перезагрузки
        }
      };

      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },
    [storageKey, min, max],
  );

  const classes = [];
  if (className !== undefined) {
    classes.push(className);
  }
  if (open) {
    classes.push("list--open");
  }
  if (collapsed) {
    classes.push("list--collapsed");
  }

  let style: CSSProperties | undefined;
  if (!narrow && !collapsed && width !== null) {
    style = { width: `${width}px` };
  }

  return (
    <aside className={classes.join(" ")} style={style} {...rest}>
      {children}
      <div className="list__resize" aria-hidden="true" onPointerDown={resizeStart} />
    </aside>
  );
}
