import {
  createContext,
  useContext,
  useMemo,
  useRef,
  type CSSProperties,
  type FormEvent,
  type HTMLAttributes,
  type PointerEvent as ReactPointerEvent,
  type ReactElement,
  type ReactNode,
  type RefObject,
} from "react";

import "./Layout.css";
import { useStoredWidth } from "./useStoredWidth";

type Marked = {
  /** Метка для тестов: data-testid. */
  mark?: string | undefined;
  children?: ReactNode;
};

type PageProps = Marked & Omit<HTMLAttributes<HTMLDivElement>, "className" | "children">;

/** Страница: шапка, полоса уведомлений и тело. data-* атрибуты страницы
 * (source, version, can-edit) пробрасываются как есть.
 * Единственное место, где существуют классы `page*`. */
export function Page({ mark, children, ...rest }: PageProps): ReactElement {
  return (
    <div className="page" data-testid={mark} {...rest}>
      {children}
    </div>
  );
}

/** Полоса уведомлений между шапкой и телом: Alert'ы страницы. */
export function PageNotices({ children }: { children?: ReactNode }): ReactElement {
  return <div className="page__notices">{children}</div>;
}

/** Ключи запомненных ширин колонок: одни на все страницы. */
const WidthKey = {
  PANE: "studio.pane-width",
  DETAIL: "studio.detail-width",
} as const;

type ColumnResize = {
  resize: (width: number) => void;
  settle: (width: number) => void;
};

type BodyResize = {
  pane: ColumnResize;
  detail: ColumnResize;
};

const BodyResizeContext = createContext<BodyResize | undefined>(undefined);

/** Ширина колонки строкой для CSS: в пределах минимума и пользовательского максимума. */
function column(width: number | undefined, kind: "pane" | "detail"): string | undefined {
  if (width === undefined) {
    return undefined;
  }

  return `clamp(var(--w-${kind}-min), ${Math.round(width)}px, var(--w-${kind}-user-max))`;
}

/** Тело страницы: колонки панели, сцены и деталей появляются вместе с
 * элементами Pane/Scene/Detail внутри (CSS :has); ширину колонок задаёт
 * запомненный выбор пользователя. */
export function PageBody({ children }: { children: ReactNode }): ReactElement {
  const pane = useStoredWidth(WidthKey.PANE);
  const detail = useStoredWidth(WidthKey.DETAIL);
  const context = useMemo<BodyResize>(
    () => ({
      pane: { resize: pane.resize, settle: pane.settle },
      detail: { resize: detail.resize, settle: detail.settle },
    }),
    [pane.resize, pane.settle, detail.resize, detail.settle],
  );

  const style: CSSProperties = {};
  const paneColumn = column(pane.width, "pane");
  if (paneColumn !== undefined) {
    Object.assign(style, { "--w-pane-col": paneColumn });
  }
  const detailColumn = column(detail.width, "detail");
  if (detailColumn !== undefined) {
    Object.assign(style, { "--w-detail-col": detailColumn });
  }

  return (
    <BodyResizeContext.Provider value={context}>
      <div
        className="page__body"
        data-pane-width={pane.width}
        data-detail-width={detail.width}
        style={style}
      >
        {children}
      </div>
    </BodyResizeContext.Provider>
  );
}

type GripProps = {
  /** С какого края колонки полоса захвата и как считать ширину. */
  side: "left" | "right";
  label: string;
  mark: string;
  target: ColumnResize | undefined;
  column: RefObject<HTMLElement | null>;
};

/** Полоса захвата у края колонки: за неё колонку тянут; ширина считается
 * от противоположного края. */
function Grip({ side, label, mark, target, column: box }: GripProps): ReactElement | null {
  const pointer = useRef<number | null>(null);
  if (target === undefined) {
    return null;
  }

  const widthAt = (clientX: number): number | undefined => {
    const rect = box.current?.getBoundingClientRect();
    if (rect === undefined) {
      return undefined;
    }

    if (side === "left") {
      return rect.right - clientX;
    }

    return clientX - rect.left;
  };

  const down = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if (event.button !== 0) {
      return;
    }

    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    pointer.current = event.pointerId;
  };

  const move = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if (pointer.current !== event.pointerId) {
      return;
    }

    const width = widthAt(event.clientX);
    if (width !== undefined) {
      target.resize(width);
    }
  };

  const up = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if (pointer.current !== event.pointerId) {
      return;
    }

    event.currentTarget.releasePointerCapture(event.pointerId);
    pointer.current = null;
    const width = widthAt(event.clientX);
    if (width !== undefined) {
      target.settle(width);
    }
  };

  return (
    <div
      className={`page__grip page__grip--${side}`}
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      data-testid={mark}
      onPointerDown={down}
      onPointerMove={move}
      onPointerUp={up}
    />
  );
}

type ColumnProps = Marked & {
  /** aria-label колонки: чем она является для читалки. */
  label?: string | undefined;
};

/** Левая панель: за правый край тянется, ширина запоминается. */
export function Pane({ mark, label, children }: ColumnProps): ReactElement {
  const resizing = useContext(BodyResizeContext);
  const aside = useRef<HTMLElement | null>(null);

  return (
    <aside className="page__pane" data-testid={mark} aria-label={label} ref={aside}>
      {children}
      <Grip side="right" label="resize the pane" mark="pane-grip" target={resizing?.pane} column={aside} />
    </aside>
  );
}

/** Закреплённая полоса панели: заголовок, поиск, действия. */
export function PaneBar({ mark, children }: Marked): ReactElement {
  return (
    <div className="page__pane-bar" data-testid={mark}>
      {children}
    </div>
  );
}

/** Прокручиваемое тело панели. */
export function PaneBody({ mark, children }: Marked): ReactElement {
  return (
    <div className="page__pane-body" data-testid={mark}>
      {children}
    </div>
  );
}

type SceneProps = Marked &
  Omit<HTMLAttributes<HTMLElement>, "className" | "children"> & {
    /** Текстовая сцена с прокруткой вместо холста. */
    panel?: boolean;
  };

/** Сцена: полосы сверху (vitals, тулбары), последний ребёнок — холст или
 * вид на всю оставшуюся высоту. */
export function Scene({ mark, panel = false, children, ...rest }: SceneProps): ReactElement {
  const classes = ["page__scene"];
  if (panel) {
    classes.push("page__scene--panel");
  }

  return (
    <main className={classes.join(" ")} data-testid={mark} {...rest}>
      {children}
    </main>
  );
}

/** Вид сцены: область под полосами; scroll — прокручивается сама (таблица, таймлайн). */
export function SceneView({ mark, scroll = false, children }: Marked & { scroll?: boolean }): ReactElement {
  const classes = ["page__view"];
  if (scroll) {
    classes.push("page__view--scroll");
  }

  return (
    <div className={classes.join(" ")} data-testid={mark}>
      {children}
    </div>
  );
}

/** Панель деталей справа: за левый край тянется, ширина запоминается. */
export function Detail({ mark, label, children }: ColumnProps): ReactElement {
  const resizing = useContext(BodyResizeContext);
  const aside = useRef<HTMLElement | null>(null);

  return (
    <aside className="page__detail" data-testid={mark} aria-label={label} ref={aside}>
      <Grip side="left" label="resize the details" mark="detail-grip" target={resizing?.detail} column={aside} />
      <div className="page__detail-body">{children}</div>
    </aside>
  );
}

type FormProps = Marked & {
  onSubmit: (event: FormEvent) => void;
};

/** Форма — стопка полей с зазором из шкалы. */
export function Form({ onSubmit, mark, children }: FormProps): ReactElement {
  return (
    <form className="stack" onSubmit={onSubmit} data-testid={mark}>
      {children}
    </form>
  );
}

type RowProps = Marked & {
  wrap?: boolean;
};

/** Ряд с зазором из шкалы. */
export function Row({ wrap = false, mark, children }: RowProps): ReactElement {
  const classes = ["row"];
  if (wrap) {
    classes.push("row--wrap");
  }

  return (
    <div className={classes.join(" ")} data-testid={mark}>
      {children}
    </div>
  );
}

type IndexProps = Marked & Omit<HTMLAttributes<HTMLDivElement>, "className" | "children">;

/** Страница-список по центру: источники, расшаренные диаграммы. */
/** Страница-список: скроллится сама на всю высоту окна, содержимое по центру
 * шириной страницы. */
export function Index({ mark, children, ...rest }: IndexProps): ReactElement {
  return (
    <div className="index" data-testid={mark} {...rest}>
      <div className="index__body">{children}</div>
    </div>
  );
}

export function IndexHead({ children }: { children: ReactNode }): ReactElement {
  return <header className="index__head">{children}</header>;
}
