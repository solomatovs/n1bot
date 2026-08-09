import { useEffect, useRef, useState } from "react";
import { Markdown } from "@/components/markdown";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { FileQuestion, Maximize2, Minus, Plus, RotateCcw, X } from "lucide-react";

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

// Пан/зум-обёртка над готовым SVG; своё состояние на каждый вьюпорт.
// wheelZoom включают только там, где колесо не отнимает прокрутку страницы.
function Viewport({ svg, height, children, wheelZoom, grow, inset }) {
  const [view, setView] = useState({ k: 1, x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const boxRef = useRef(null);
  const dragRef = useRef(null);

  useEffect(() => {
    if (!wheelZoom) return;
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
  }, [wheelZoom]);

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

  return (
    <div
      ref={boxRef}
      className={
        "relative overflow-hidden select-none touch-none w-full" +
        (grow ? " flex-1 min-h-0" : "")
      }
      style={{ height, cursor: dragging ? "grabbing" : "grab" }}
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
        // единственное исключение: сюда попадает только вывод mermaid.render
        dangerouslySetInnerHTML={{ __html: svg }}
      />
      <div className={"absolute z-10 flex gap-1 top-4 " + (inset ? "" : "right-4")}
           style={inset ? { right: inset } : undefined}>
        <Button variant="ghost" size="icon" onClick={zoomIn} title="Приблизить" aria-label="Приблизить">
          <Plus />
        </Button>
        <Button variant="ghost" size="icon" onClick={zoomOut} title="Отдалить" aria-label="Отдалить">
          <Minus />
        </Button>
        <Button variant="ghost" size="icon" onClick={reset} title="Сбросить вид" aria-label="Сбросить вид">
          <RotateCcw />
        </Button>
        {children}
      </div>
    </div>
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
      setError("mermaid.js не загружен: соберите webassets (make webassets)");
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
            диаграмма не отрисована: {error}
          </div>
          <pre className="overflow-auto text-xs font-mono">{spec}</pre>
        </div>
      ) : !svg ? (
        <div className="px-2 py-1 text-xs text-muted-foreground">
          отрисовка диаграммы…
        </div>
      ) : (
        <Dialog>
          {/* панель отдаёт всю высоту; колесо здесь листает страницу */}
          <Viewport grow wheelZoom={false} svg={svg}>
            <DialogTrigger asChild>
              <Button variant="ghost" size="icon" title="Во весь экран" aria-label="Во весь экран">
                <Maximize2 />
              </Button>
            </DialogTrigger>
          </Viewport>
          <DialogContent className="canvas-fullscreen max-w-[90vw] max-h-[85vh] p-0 overflow-hidden">
            {/* радиксовый close отдельной кнопкой ломает единый ряд управления:
                прячем его и ставим закрытие в тот же ряд, что +/-/сброс */}
            <style>{".canvas-fullscreen > button.absolute{display:none!important;}"}</style>
            {/* заголовок нужен radix для доступности, но строку он не занимает */}
            <DialogTitle className="sr-only">{content.label}</DialogTitle>
            <Viewport svg={svg} height="80vh" wheelZoom>
              <DialogClose asChild>
                <Button variant="ghost" size="icon" title="Закрыть" aria-label="Закрыть">
                  <X />
                </Button>
              </DialogClose>
            </Viewport>
          </DialogContent>
        </Dialog>
      )}
    </div>
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
        файл не прочитан: {content.label}
      </div>
    );
  }

  return (
    <div className="flex-1 min-h-0 overflow-auto p-3">
      <Markdown allowHtml={false} latex={false}>
        {text}
      </Markdown>
    </div>
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
      title={`Скачать ${name}`}
    >
      {name}
    </a>
  );
}


function Notice({ content }) {
  return (
    <div className="flex flex-col gap-2 p-3">
      <div className="flex items-center gap-2 text-sm font-medium">
        <FileQuestion />
        <span className="truncate">{content.label}</span>
      </div>
      <div className="text-xs text-muted-foreground">{content.note}</div>
    </div>
  );
}

// Живой вывод инструмента: хвост окна без шапки, кнопки размера шрифта
// поверх — как кнопки зума у диаграммы. Держится за низ, пока пользователь
// сам не отмотал вверх.
function StreamTail({ content }) {
  const boxRef = useRef(null);
  const stickRef = useRef(true);
  const [fontPx, setFontPx] = useState(12);

  const onScroll = () => {
    const box = boxRef.current;
    if (!box) return;
    stickRef.current =
      box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  };

  useEffect(() => {
    const box = boxRef.current;
    if (!box || !stickRef.current) return;
    box.scrollTop = box.scrollHeight;
  }, [content.text, content.nonce]);

  const bigger = () => setFontPx((size) => Math.min(24, size + 2));
  const smaller = () => setFontPx((size) => Math.max(8, size - 2));
  const reset = () => setFontPx(12);

  return (
    <div className="flex-1 min-h-0 flex flex-col relative">
      <div className="absolute z-10 flex gap-1 top-4 right-4">
        <Button variant="ghost" size="icon" onClick={bigger} title="Крупнее" aria-label="Крупнее">
          <Plus />
        </Button>
        <Button variant="ghost" size="icon" onClick={smaller} title="Мельче" aria-label="Мельче">
          <Minus />
        </Button>
        <Button variant="ghost" size="icon" onClick={reset} title="Сбросить размер" aria-label="Сбросить размер">
          <RotateCcw />
        </Button>
      </div>
      <div
        ref={boxRef}
        onScroll={onScroll}
        className="flex-1 min-h-0 overflow-auto p-3"
      >
        <pre
          className="font-mono whitespace-pre-wrap break-words"
          style={{ fontSize: fontPx }}
        >
          {content.text || " "}
        </pre>
      </div>
    </div>
  );
}

function Body({ content }) {
  switch (content.kind) {
    case "mermaid":
      return <Diagram content={content} />;
    case "image":
      return (
        <div className="flex-1 min-h-0 flex items-center justify-center p-2">
          <img
            src={content.url}
            alt={content.label}
            className="max-w-full max-h-full object-contain"
          />
        </div>
      );
    case "pdf":
      return (
        <iframe
          src={content.url}
          title={content.label}
          className="flex-1 min-h-0 w-full border-0"
        />
      );
    case "video":
      return (
        <div className="flex-1 min-h-0 flex items-center justify-center p-2">
          <video src={content.url} controls className="max-w-full max-h-full" />
        </div>
      );
    case "audio":
      return (
        <div className="p-3">
          <audio src={content.url} controls className="w-full" />
        </div>
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
          aria-label="Показать в канвасе"
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
    <div className="border border-border rounded-lg bg-card overflow-hidden w-full flex-1 min-h-0 flex flex-col relative">
      <Body content={content} />
    </div>
  );
}
