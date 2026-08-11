import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Markdown } from "@/components/markdown";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import {
  ArrowDownToLine,
  ArrowUpToLine,
  Download,
  Maximize2,
  Minus,
  Plus,
  RotateCcw,
  X,
} from "lucide-react";

const MERMAID_PATH = "/public/vendor/mermaid/mermaid.min.js";
const SWITCH_EVENT = "boba:canvas";
// пересохранение файла: открытая панель того же пути перерисовывается на месте,
// не переоткрываясь и без анимации; несущий свежее содержимое — карточка ленты
const REFRESH_EVENT = "boba:canvas-refresh";

// Единственный слот custom_js занят SSO-кнопкой, поэтому библиотеку тянет сам
// компонент: тег переиспользуется всеми диаграммами на странице.
function useMermaid(needed) {
  const [state, setState] = useState(() =>
    window.mermaid ? "ready" : "loading"
  );

  useEffect(() => {
    if (!needed || window.mermaid) {
      if (window.mermaid) setState("ready");
      return;
    }

    const rootPath =
      document.querySelector('meta[property="og:root_path"]')?.content || "";
    const src = rootPath.replace(/\/$/, "") + MERMAID_PATH;

    let script = document.querySelector(`script[src="${src}"]`);
    if (!script) {
      script = document.createElement("script");
      script.src = src;
      script.async = true;
      document.head.appendChild(script);
    }

    const onLoad = () => setState("ready");
    const onError = () => setState("failed");
    script.addEventListener("load", onLoad);
    script.addEventListener("error", onError);

    return () => {
      script.removeEventListener("load", onLoad);
      script.removeEventListener("error", onError);
    };
  }, [needed]);

  return state;
}

// mermaid отдаёт svg с фиксированными размерами и max-width; чтобы диаграмма
// занимала весь вьюпорт, правим их прямо в разметке — произвольные
// tailwind-селекторы в собранном css chainlit отсутствуют
function fitSvg(markup) {
  const opening = markup.match(/<svg[^>]*>/);
  if (!opening) return markup;

  let tag = opening[0];
  tag = tag.replace(/\s(width|height)="[^"]*"/g, "");
  tag = tag.replace(/\sstyle="[^"]*"/g, "");
  tag = tag.replace(
    "<svg",
    '<svg style="width:100%;height:100%;max-width:none" preserveAspectRatio="xMidYMid meet"'
  );
  return markup.replace(opening[0], tag);
}

// ——— Единый набор управления для всех типов канваса ———
// Один стиль кнопки, один ряд-тулбар, одна обёртка «на весь экран»: любой
// вьювер собирает свои кнопки из этих кубиков, отступить от общего вида нельзя.

function ToolButton({ onClick, title, children }) {
  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={onClick}
      title={title}
      aria-label={title}
    >
      {children}
    </Button>
  );
}

// Единая шапка сцены: один ряд кнопок в панели и на весь экран, закрытие
// всегда последнее справа. Клики по шапке не всплывают в карточку.
function StageBar({ full, status, children }) {
  const stop = (event) => event.stopPropagation();
  return (
    <div
      className="flex-shrink-0 flex items-center gap-1 py-4 px-6 border-b border-border bg-card"
      onClick={stop}
      onPointerDown={stop}
    >
      <div className="flex-1 min-w-0 truncate text-[11px] text-muted-foreground/80">
        {status}
      </div>
      {children}
      <TrailButton full={full} />
    </div>
  );
}

// Сцена: шапка сверху, тело занимает остаток и прокручивается само — шапка
// не уезжает ни в панели, ни на весь экран.
function Stage({ full, status, bar, children }) {
  return (
    <div
      className={
        "relative flex flex-col w-full overflow-hidden" +
        (full ? "" : " flex-1 min-h-0 h-full")
      }
      style={{ height: full ? "80vh" : undefined }}
    >
      <StageBar full={full} status={status}>
        {bar}
      </StageBar>
      {children}
    </div>
  );
}

// Замыкающие кнопки ряда — как в полноэкранном режиме: закрытие всегда
// последнее справа. В панели перед ним «на весь экран», а закрывает панель
// клик по спрятанному родному «назад» chainlit — его состояние живёт там.
function TrailButton({ full }) {
  if (full) {
    return (
      <DialogClose asChild>
        <Button variant="ghost" size="icon" title="Close" aria-label="Close">
          <X />
        </Button>
      </DialogClose>
    );
  }

  const closePanel = () => {
    const back = document.querySelector("#side-view-title button");
    if (back) back.click();
  };

  return (
    <>
      <DialogTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          title="Fullscreen"
          aria-label="Fullscreen"
        >
          <Maximize2 />
        </Button>
      </DialogTrigger>
      <Button
        variant="ghost"
        size="icon"
        onClick={closePanel}
        title="Close"
        aria-label="Close"
      >
        <X />
      </Button>
    </>
  );
}

// Обёртка «на весь экран» для любого содержимого: панельный и полноэкранный
// варианты живут в одном Dialog. render(full) отдаёт тело с его тулбаром;
// полноэкранное тело радикс монтирует только при открытии.
function Fullscreen({ label, children }) {
  return (
    <Dialog>
      {children(false)}
      <DialogContent className="canvas-fullscreen max-w-[90vw] max-h-[85vh] p-0 overflow-hidden">
        {/* радиксовый close отдельной кнопкой ломает единый ряд — прячем его,
            закрытие стоит в тулбаре тем же стилем, что и остальные кнопки */}
        <style>{".canvas-fullscreen > button.absolute{display:none!important;}"}</style>
        <DialogTitle className="sr-only">{label}</DialogTitle>
        {children(true)}
      </DialogContent>
    </Dialog>
  );
}

// Пан/зум-сцена для визуального содержимого (svg диаграммы, картинка). В панели
// колесо листает страницу, на весь экран — зумит. controls — кнопки типа перед
// общей замыкающей.
function ZoomStage({ full, controls, children }) {
  const [view, setView] = useState({ k: 1, x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const boxRef = useRef(null);
  const dragRef = useRef(null);

  useEffect(() => {
    if (!full) return;
    const box = boxRef.current;
    if (!box) return;
    const onWheel = (e) => {
      e.preventDefault();
      const rect = box.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      setView((v) => {
        const k = Math.min(3, Math.max(0.25, v.k * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
        return {
          k,
          x: mx - ((mx - v.x) * k) / v.k,
          y: my - ((my - v.y) * k) / v.k,
        };
      });
    };
    box.addEventListener("wheel", onWheel, { passive: false });
    return () => box.removeEventListener("wheel", onWheel);
  }, [full]);

  const onPointerDown = (e) => {
    if (e.target.closest("button")) return;
    dragRef.current = { x: e.clientX - view.x, y: e.clientY - view.y };
    setDragging(true);
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e) => {
    const drag = dragRef.current;
    if (!drag) return;
    setView((v) => ({ k: v.k, x: e.clientX - drag.x, y: e.clientY - drag.y }));
  };
  const onPointerUp = () => {
    dragRef.current = null;
    setDragging(false);
  };
  const zoomIn = () => setView((v) => ({ ...v, k: Math.min(3, v.k * 1.25) }));
  const zoomOut = () => setView((v) => ({ ...v, k: Math.max(0.25, v.k / 1.25) }));
  const reset = () => setView({ k: 1, x: 0, y: 0 });

  const bar = (
    <>
      <ToolButton onClick={zoomIn} title="Zoom in">
        <Plus />
      </ToolButton>
      <ToolButton onClick={zoomOut} title="Zoom out">
        <Minus />
      </ToolButton>
      <ToolButton onClick={reset} title="Reset view">
        <RotateCcw />
      </ToolButton>
      {controls}
    </>
  );

  return (
    <Stage full={full} bar={bar}>
      <div
        ref={boxRef}
        className="relative overflow-hidden select-none touch-none w-full flex-1 min-h-0"
        style={{ cursor: dragging ? "grabbing" : "grab" }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        <div
          className="w-full h-full"
          style={{
            transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})`,
            transformOrigin: "0 0",
          }}
        >
          {children}
        </div>
      </div>
    </Stage>
  );
}

// Скролл-сцена для текстового содержимого (лог, поток, markdown): размер шрифта
// теми же кнопками. stick держит низ при доливе строк — прокрутка правится до
// кадра (useLayoutEffect), поэтому новая строка не дёргает вьюпорт.
function ScrollStage({
  full,
  stick,
  deps,
  render,
  controls,
  status,
  boxRef: outerBoxRef,
  onEdge,
}) {
  const localBoxRef = useRef(null);
  const boxRef = outerBoxRef || localBoxRef;
  const stickRef = useRef(true);
  const [fontPx, setFontPx] = useState(12);

  const onScroll = () => {
    const box = boxRef.current;
    if (!box) return;
    stickRef.current = box.scrollHeight - box.scrollTop - box.clientHeight < 40;

    // непрерывная прокрутка: у кромки владелец подтягивает соседнее окно
    if (!onEdge) return;
    if (box.scrollTop < 200) {
      onEdge("top");
      return;
    }
    if (box.scrollHeight - box.scrollTop - box.clientHeight < 200) {
      onEdge("bottom");
    }
  };

  useLayoutEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    // без прилипания новое содержимое читается с начала окна
    if (!stick) {
      box.scrollTop = 0;
      return;
    }
    if (!stickRef.current) return;
    box.scrollTop = box.scrollHeight;
  }, [stick, fontPx, ...deps]);

  const bigger = () => setFontPx((size) => Math.min(24, size + 2));
  const smaller = () => setFontPx((size) => Math.max(8, size - 2));
  const reset = () => setFontPx(12);

  const bar = (
    <>
      <ToolButton onClick={bigger} title="Larger">
        <Plus />
      </ToolButton>
      <ToolButton onClick={smaller} title="Smaller">
        <Minus />
      </ToolButton>
      <ToolButton onClick={reset} title="Reset size">
        <RotateCcw />
      </ToolButton>
      {controls}
    </>
  );

  return (
    <Stage full={full} status={status} bar={bar}>
      <div
        ref={boxRef}
        onScroll={stick || onEdge ? onScroll : undefined}
        className="flex-1 min-h-0 overflow-auto p-3"
      >
        {render(fontPx)}
      </div>
    </Stage>
  );
}

// Родное содержимое (pdf/видео/аудио) со своими контролами: над ним только
// общая шапка с «на весь экран» / «закрыть».
function NativeStage({ full, children }) {
  return (
    <Stage full={full}>
      {children}
    </Stage>
  );
}

function Diagram({ content }) {
  const [svg, setSvg] = useState("");
  const [error, setError] = useState(null);
  const [dark, setDark] = useState(() =>
    document.documentElement.classList.contains("dark")
  );
  const idRef = useRef(`mmd-${Math.random().toString(36).slice(2)}`);
  const hostRef = useRef(null);
  const reportedRef = useRef("");
  const library = useMermaid(true);
  const spec = content.text || "";

  // синтаксис знает только mermaid в браузере: diagram_save ждёт этот вердикт
  // по nonce, чтобы вернуть LLM ошибку рендера; повторные рендеры не шумят
  const report = (ok, message) => {
    if (!content.nonce || reportedRef.current === content.nonce) return;
    reportedRef.current = content.nonce;
    callAction({
      name: "canvas_render_status",
      payload: { nonce: content.nonce, ok, error: message || "" },
    });
  };

  useEffect(() => {
    const root = document.documentElement;
    const observer = new MutationObserver(() =>
      setDark(root.classList.contains("dark"))
    );
    observer.observe(root, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (library === "failed") {
      setError("mermaid.js is not loaded: build webassets (make webassets)");
      report(false, "mermaid.js is not loaded");
      return;
    }
    if (library !== "ready") return;

    let cancelled = false;
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      suppressErrorRendering: true,
      theme: dark ? "dark" : "default",
    });
    // третий аргумент — контейнер в DOM: mindmap/timeline меряют разметку
    // при рендере и падают во временном неприкреплённом элементе
    window.mermaid
      .render(idRef.current, spec, hostRef.current || undefined)
      .then((out) => {
        if (cancelled) return;
        if (hostRef.current) hostRef.current.innerHTML = "";
        setSvg(fitSvg(out.svg));
        setError(null);
        report(true, "");
      })
      .catch((e) => {
        if (cancelled) return;
        if (hostRef.current) hostRef.current.innerHTML = "";
        setSvg("");
        setError(String((e && e.message) || e));
        report(false, String((e && e.message) || e));
      });
    return () => {
      cancelled = true;
    };
  }, [spec, dark, library]);

  return (
    <div className="flex-1 min-h-0 flex flex-col relative">
      <div
        ref={hostRef}
        aria-hidden="true"
        className="absolute overflow-hidden"
        style={{ left: -10000, top: 0, width: 1200, height: 900 }}
      />
      {error ? (
        <div className="px-2 py-1 flex flex-col gap-2 overflow-auto">
          <div className="text-xs text-muted-foreground">
            diagram not rendered: {error}
          </div>
          <pre className="overflow-auto text-xs font-mono">{spec}</pre>
        </div>
      ) : !svg ? (
        <div className="px-2 py-1 text-xs text-muted-foreground">
          rendering the diagram…
        </div>
      ) : (
        <Fullscreen label={content.label}>
          {(full) => (
            <ZoomStage full={full}>
              <div
                className="w-full h-full"
                // единственное исключение: сюда попадает только вывод mermaid.render
                dangerouslySetInnerHTML={{ __html: svg }}
              />
            </ZoomStage>
          )}
        </Fullscreen>
      )}
    </div>
  );
}

function ImageView({ content }) {
  return (
    <Fullscreen label={content.label}>
      {(full) => (
        <ZoomStage full={full}>
          <img
            src={content.url}
            alt={content.label}
            draggable={false}
            className="w-full h-full object-contain pointer-events-none"
          />
        </ZoomStage>
      )}
    </Fullscreen>
  );
}

// Поток журнала: сервер пушит хвост окна; кнопка скачивает весь .log тем же
// файловым роутом. В окно во фронт целиком файл любого размера не попадает.
// Поток журнала как less: непрерывная прокрутка окнами, файла в DOM целиком
// нет. Пока держимся низа живого вывода, кадры хвоста шлёт насос (follow);
// прокрутка вверх переводит в browse — цепочку окон встык, соседние
// подтягиваются у кромок, дальний край подрезается. Докрутил обратно до
// конца живого файла — снова follow.
function StreamTail({ content }) {
  const MAX_SEGMENTS = 8;

  const [view, setView] = useState(content);
  const [chain, setChain] = useState(null);
  const boxRef = useRef(null);
  const busyRef = useRef(false);
  const anchorRef = useRef(null);

  // пуш с сервера (открытие, «в начало», «в конец», кадры насоса) задаёт
  // новую точку отсчёта: накопленная цепочка окон устаревает
  useEffect(() => {
    setView(content);
    setChain(null);
  }, [content.nonce]);

  const callId = (content.path || "").replace("stream://", "");
  const pos = view.stream || {
    offset: 0, end: 0, size: 0, window: 65536, closed: true, follow: false,
  };
  const browsing = chain !== null;

  const current = () => {
    if (chain) return chain;
    return {
      segments: [{ offset: pos.offset, end: pos.end, text: view.text || "" }],
      size: pos.size,
      closed: pos.closed,
    };
  };

  const segOf = (answer) => ({
    offset: answer.stream.offset,
    end: answer.stream.end,
    text: answer.text || "",
  });

  const fetchWindow = async (payload) => {
    const answer = await callAction({
      name: "canvas_stream_window",
      payload: { call_id: callId, ...payload },
    });
    const next = answer && answer.response;
    if (!next || !next.stream) return null;
    return next;
  };

  const loadBefore = async () => {
    const cur = current();
    const first = cur.segments[0];
    if (first.offset <= 0) return;

    busyRef.current = true;
    try {
      const next = await fetchWindow({ before: first.offset });
      if (!next) return;
      const box = boxRef.current;
      anchorRef.current = box ? box.scrollHeight - box.scrollTop : null;
      setChain({
        segments: [segOf(next), ...cur.segments],
        size: next.stream.size,
        closed: next.stream.closed,
      });
    } finally {
      busyRef.current = false;
    }
  };

  const backToLive = () => {
    setChain(null);
    callAction({ name: "canvas_stream", payload: { call_id: callId } });
  };

  const loadAfter = async () => {
    const cur = current();
    // не догружает вниз только follow-хвост живого вывода — его шлёт насос;
    // любое окно не у хвоста (живое или закрытое) листается дальше
    if (!chain && !pos.closed && pos.end >= pos.size) return;
    const last = cur.segments[cur.segments.length - 1];

    busyRef.current = true;
    try {
      if (last.end >= cur.size) {
        if (!cur.closed) backToLive();
        return;
      }
      const next = await fetchWindow({ offset: last.end });
      if (!next || next.stream.end <= last.end) {
        if (!cur.closed) backToLive();
        return;
      }
      setChain({
        segments: [...cur.segments, segOf(next)],
        size: next.stream.size,
        closed: next.stream.closed,
      });
    } finally {
      busyRef.current = false;
    }
  };

  const onEdge = (direction) => {
    if (busyRef.current) return;
    if (direction === "top") {
      loadBefore();
      return;
    }
    loadAfter();
  };

  // компенсация prepend: контент вырос сверху, позиция держится якорем от низа
  useLayoutEffect(() => {
    const box = boxRef.current;
    const anchor = anchorRef.current;
    if (!box || anchor == null) return;
    anchorRef.current = null;
    box.scrollTop = box.scrollHeight - anchor;
  }, [chain]);

  // подрезка дальнего края отдельным тиком: обрезка снизу позицию не трогает,
  // обрезка сверху компенсируется тем же якорем от низа
  useEffect(() => {
    if (!chain || chain.segments.length <= MAX_SEGMENTS) return;
    const box = boxRef.current;
    const nearTop = box && box.scrollTop < box.clientHeight;
    if (nearTop) {
      setChain({ ...chain, segments: chain.segments.slice(0, MAX_SEGMENTS) });
      return;
    }
    anchorRef.current = box ? box.scrollHeight - box.scrollTop : null;
    setChain({ ...chain, segments: chain.segments.slice(-MAX_SEGMENTS) });
  }, [chain]);

  const shown = current();
  const text = browsing
    ? shown.segments.map((segment) => segment.text).join("")
    : view.text || " ";
  // прилипание к низу — только когда показан хвост живого вывода
  const live = !browsing && !pos.closed && pos.end >= pos.size;

  // follow-пуш («в конец», кадры насоса) должен встать на низ окна: раннер
  // пересоздаёт компонент на каждый пуш, поэтому интент едет с сервера в
  // props. Эффект родителя выполняется после ScrollStage и побеждает его верх.
  useLayoutEffect(() => {
    if (!pos.follow) return;
    const box = boxRef.current;
    if (!box) return;
    box.scrollTop = box.scrollHeight;
  }, [view.nonce]);

  // «в начало» и «в конец» — пуши сервера: цепочка окон сбрасывается пушем
  const toStart = () =>
    callAction({ name: "canvas_stream", payload: { call_id: callId } });
  const toEnd = () =>
    callAction({
      name: "canvas_stream",
      payload: { call_id: callId, follow: true },
    });

  // скачивание — тем же роутом отдачи файлов; сервер шлёт весь .log целиком
  const download = () => {
    if (!view.url) return;
    const link = document.createElement("a");
    link.href = view.url;
    link.download = `${view.label || callId || "output"}.log`;
    document.body.appendChild(link);
    link.click();
    link.remove();
  };

  const controls = (
    <>
      <ToolButton onClick={toStart} title="Go to the file start">
        <ArrowUpToLine />
      </ToolButton>
      <ToolButton onClick={toEnd} title="Go to the file end and follow output">
        <ArrowDownToLine />
      </ToolButton>
      {view.url ? (
        <ToolButton onClick={download} title="Download output">
          <Download />
        </ToolButton>
      ) : null}
    </>
  );

  return (
    <Fullscreen label={view.label}>
      {(full) => (
        <ScrollStage
          full={full}
          stick={live}
          deps={browsing ? [] : [view.text, view.nonce]}
          boxRef={boxRef}
          onEdge={onEdge}
          controls={controls}
          render={(fontPx) => (
            <pre
              className="font-mono whitespace-pre-wrap break-words"
              style={{ fontSize: fontPx }}
            >
              {text}
            </pre>
          )}
        />
      )}
    </Fullscreen>
  );
}

function TextFile({ content }) {
  const [text, setText] = useState("");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setText("");
    setFailed(false);
    fetch(content.url)
      .then((answer) => {
        if (!answer.ok) throw new Error(String(answer.status));
        return answer.text();
      })
      .then((body) => {
        if (!cancelled) setText(body);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [content.url]);

  if (failed) {
    return (
      <div className="p-3 text-xs text-muted-foreground">
        file is not readable: {content.label}
      </div>
    );
  }

  return (
    <Fullscreen label={content.label}>
      {(full) => (
        <ScrollStage
          full={full}
          deps={[text]}
          render={(fontPx) => (
            <div style={{ fontSize: fontPx }}>
              <Markdown allowHtml={false} latex={false}>
                {text}
              </Markdown>
            </div>
          )}
        />
      )}
    </Fullscreen>
  );
}

// Имя файла ссылкой: у диаграммы исходник лежит в самой карточке, у прочих
// файлов — по ссылке на storage.
function SourceLink({ content }) {
  const name = content.path.split("/").pop() || content.label;
  const [href, setHref] = useState(content.url || "");

  useEffect(() => {
    if (!content.text) {
      setHref(content.url || "");
      return;
    }

    const blob = new Blob([content.text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    setHref(url);
    return () => URL.revokeObjectURL(url);
  }, [content.text, content.url]);

  return (
    <a
      href={href}
      download={name}
      onClick={(event) => event.stopPropagation()}
      className="text-xs text-muted-foreground hover:text-foreground underline underline-offset-2 truncate w-fit max-w-full"
      title={`Download ${name}`}
    >
      {name}
    </a>
  );
}


// Без шапки: иконка с именем налезали бы на кнопку закрытия панели слева
// сверху. Текст по центру — сам объясняет, почему содержимого нет.
function Notice({ content }) {
  return (
    <div className="flex-1 min-h-0 flex items-center justify-center p-6">
      <div className="max-w-md text-center text-sm text-muted-foreground">
        {content.note}
      </div>
    </div>
  );
}

function Body({ content }) {
  switch (content.kind) {
    case "mermaid":
      return <Diagram content={content} />;
    case "image":
      return <ImageView content={content} />;
    case "pdf":
      return (
        <NativeStage>
          <iframe
            src={content.url}
            title={content.label}
            className="flex-1 min-h-0 w-full border-0"
          />
        </NativeStage>
      );
    case "video":
      return (
        <NativeStage>
          <div className="flex-1 min-h-0 flex items-center justify-center p-2">
            <video src={content.url} controls className="max-w-full max-h-full" />
          </div>
        </NativeStage>
      );
    case "audio":
      return (
        <NativeStage>
          <div className="p-3">
            <audio src={content.url} controls className="w-full" />
          </div>
        </NativeStage>
      );
    case "text":
      return <TextFile content={content} />;
    case "stream":
      return <StreamTail content={content} />;
    default:
      return <Notice content={content} />;
  }
}

// Единственный компонент панели: рисует любое содержимое и сам переключает
// файлы. Смена файла идёт событием из ленты, а не новым элементом с сервера —
// иначе chainlit пересоздал бы панель и снова проиграл анимацию открытия.
export default function CanvasView() {
  const [content, setContent] = useState(props);

  useEffect(() => setContent(props), [props.path, props.nonce]);

  // новая карточка того же файла: сообщает открытой панели свежее содержимое,
  // чтобы та перерисовалась на месте. Панель закрыта или на другом файле —
  // событие просто некому поймать
  useEffect(() => {
    if (!props.preview) return;
    window.dispatchEvent(
      new CustomEvent(REFRESH_EVENT, {
        detail: { path: content.path, content },
      })
    );
  }, [props.preview, content]);

  // панель слушает те же обновления: тот же путь — меняем содержимое без
  // переоткрытия, чужой путь или закрытая панель события не касаются
  useEffect(() => {
    if (props.preview) return;

    const onRefresh = (event) => {
      const detail = event.detail || {};
      if (!detail.content || detail.path !== content.path) return;
      setContent(detail.content);
    };

    window.addEventListener(REFRESH_EVENT, onRefresh);
    return () => window.removeEventListener(REFRESH_EVENT, onRefresh);
  }, [content.path, props.preview]);

  // карточка в ленте: тот же рендер, но кликом показывает файл в панели
  const openInCanvas = () => {
    if (document.getElementById("side-view-content")) {
      window.dispatchEvent(
        new CustomEvent(SWITCH_EVENT, { detail: { path: content.path } })
      );
      return;
    }

    callAction({ name: "canvas_open", payload: { path: content.path } });
  };

  useEffect(() => {
    if (props.preview) return;

    const onSwitch = (event) => {
      const path = event.detail && event.detail.path;
      if (!path || path === content.path) return;

      callAction({ name: "canvas_content", payload: { path } }).then((answer) => {
        const next = answer && answer.response;
        if (next && next.kind) setContent(next);
      });
    };

    window.addEventListener(SWITCH_EVENT, onSwitch);
    return () => window.removeEventListener(SWITCH_EVENT, onSwitch);
  }, [content.path, props.preview]);

  if (props.preview) {
    return (
      <div className="flex flex-col gap-1 w-full">
        {/* имя файла живёт над карточкой и скачивает исходник; внутри рамки
            шапки нет — там только сама диаграмма */}
        <SourceLink content={content} />
        <div
          className="border border-border rounded-lg bg-card overflow-hidden w-full cursor-pointer flex flex-col"
          role="button"
          tabIndex={0}
          aria-label="Show in the canvas"
          title={content.path}
          onClick={openInCanvas}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") openInCanvas();
          }}
        >
          <div className="flex flex-col" style={{ height: 260 }}>
            <Body content={content} />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="border border-border rounded-lg bg-card overflow-hidden w-full flex-1 min-h-0 h-full flex flex-col relative">
      {/* Родной заголовок панели спрятан: закрытие живёт в ряду кнопок
          шапки, как в полноэкранном режиме (клик пробрасывается его кнопке).
          min-h-0 в flex-цепочке chainlit запирает прокрутку внутри сцены —
          шапка не уезжает вместе с содержимым */}
      <style>{`
        #side-view-title { display: none; }
        #side-view-content { min-height: 0; }
        #side-view-content > div {
          display: flex; flex-direction: column; flex: 1 1 0%; min-height: 0;
        }
      `}</style>
      <Body content={content} />
    </div>
  );
}
