import {
  createContext,
  useCallback,
  useContext,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent,
  type HTMLAttributes,
  type PointerEvent as ReactPointerEvent,
  type ReactElement,
  type ReactNode,
} from "react";

import "./Layout.css";

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

type BodyProps = {
  pane: boolean;
  detail: boolean;
  children: ReactNode;
};

/** Ширина панели деталей, выбранная пользователем: хранится в браузере и
 * действует на всех страницах каталога; пределы задаёт CSS. */
const DetailWidth = {
  KEY: "catalog.detail-width",

  load(): number | undefined {
    let raw: string | null = null;
    try {
      raw = window.localStorage.getItem(DetailWidth.KEY);
    } catch {
      return undefined;
    }

    if (raw === null) {
      return undefined;
    }

    const width = Number(raw);
    if (!Number.isFinite(width) || width <= 0) {
      return undefined;
    }

    return width;
  },

  save(width: number): void {
    try {
      window.localStorage.setItem(DetailWidth.KEY, String(Math.round(width)));
    } catch {
      return;
    }
  },

  column(width: number | undefined): string | undefined {
    if (width === undefined) {
      return undefined;
    }

    return `clamp(var(--w-detail-min), ${Math.round(width)}px, var(--w-detail-user-max))`;
  },
};

type DetailResize = {
  /** Ширина, пока тянут; в конце — запомнить. */
  resize: (width: number) => void;
  settle: (width: number) => void;
};

const DetailResizeContext = createContext<DetailResize | undefined>(undefined);

/** Тело страницы: колонки панели, сцены и деталей включаются флагами;
 * ширину колонки деталей задаёт запомненный выбор пользователя. */
export function PageBody({ pane, detail, children }: BodyProps): ReactElement {
  const [width, setWidth] = useState<number | undefined>(() => DetailWidth.load());

  const resize = useCallback((next: number) => {
    setWidth(next);
  }, []);

  const settle = useCallback((next: number) => {
    setWidth(next);
    DetailWidth.save(next);
  }, []);

  const [context] = useState<DetailResize>(() => ({ resize, settle }));
  const column = DetailWidth.column(width);
  let style: CSSProperties | undefined = undefined;
  if (column !== undefined) {
    style = { "--w-detail-col": column } as CSSProperties;
  }

  return (
    <DetailResizeContext.Provider value={context}>
      <div
        className="page__body"
        data-pane={pane}
        data-detail={detail}
        data-detail-width={width}
        style={style}
      >
        {children}
      </div>
    </DetailResizeContext.Provider>
  );
}

export function Pane({ mark, children }: Marked): ReactElement {
  return (
    <aside className="page__pane" data-testid={mark}>
      {children}
    </aside>
  );
}

type SceneProps = Marked & {
  /** Текстовая сцена с прокруткой вместо холста. */
  panel?: boolean;
};

export function Scene({ mark, panel = false, children }: SceneProps): ReactElement {
  const classes = ["page__scene"];
  if (panel) {
    classes.push("page__scene--panel");
  }

  return (
    <main className={classes.join(" ")} data-testid={mark}>
      {children}
    </main>
  );
}

/** Панель деталей справа: за левый край тянется, ширина запоминается. */
export function Detail({ mark, children }: Marked): ReactElement {
  const resizing = useContext(DetailResizeContext);
  const aside = useRef<HTMLElement | null>(null);
  const pointer = useRef<number | null>(null);

  const widthAt = (clientX: number): number | undefined => {
    const box = aside.current?.getBoundingClientRect();
    if (box === undefined) {
      return undefined;
    }

    return box.right - clientX;
  };

  const gripDown = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if (event.button !== 0) {
      return;
    }

    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    pointer.current = event.pointerId;
  };

  const gripMove = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if (pointer.current !== event.pointerId || resizing === undefined) {
      return;
    }

    const width = widthAt(event.clientX);
    if (width !== undefined) {
      resizing.resize(width);
    }
  };

  const gripUp = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if (pointer.current !== event.pointerId || resizing === undefined) {
      return;
    }

    event.currentTarget.releasePointerCapture(event.pointerId);
    pointer.current = null;
    const width = widthAt(event.clientX);
    if (width !== undefined) {
      resizing.settle(width);
    }
  };

  return (
    <aside className="page__detail" data-testid={mark} ref={aside}>
      {resizing !== undefined && (
        <div
          className="page__detail-grip"
          role="separator"
          aria-orientation="vertical"
          aria-label="resize the details"
          data-testid="detail-grip"
          onPointerDown={gripDown}
          onPointerMove={gripMove}
          onPointerUp={gripUp}
        />
      )}
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
