import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Markdown } from "@/components/markdown";
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

// Браузерные API держим в одной точке: при серверном рендере их нет, а
// разбросанные по обработчикам обращения проверить негде.
const browser = {
  get doc() {
    if (typeof document === "undefined") return null;
    return document;
  },
  get win() {
    if (typeof window === "undefined") return null;
    return window;
  },
  find(selector) {
    const doc = this.doc;
    if (!doc) return null;
    return doc.querySelector(selector);
  },
  byId(id) {
    const doc = this.doc;
    if (!doc) return null;
    return doc.getElementById(id);
  },
  create(tag) {
    const doc = this.doc;
    if (!doc) return null;
    return doc.createElement(tag);
  },
  root() {
    const doc = this.doc;
    if (!doc) return null;
    return doc.documentElement;
  },
  head() {
    const doc = this.doc;
    if (!doc) return null;
    return doc.head;
  },
  body() {
    const doc = this.doc;
    if (!doc) return null;
    return doc.body;
  },
  // корень React-приложения: клики обрабатываются делегированием на нём,
  // поэтому вынесенный за его пределы узел перестал бы слышать onClick
  overlayHost() {
    const doc = this.doc;
    if (!doc) return null;
    return doc.getElementById("root") || doc.body;
  },
  global(name) {
    const win = this.win;
    if (!win) return undefined;
    return win[name];
  },
  emit(event) {
    const win = this.win;
    if (!win) return;
    win.dispatchEvent(event);
  },
  listen(name, handler) {
    const win = this.win;
    if (!win) return;
    win.addEventListener(name, handler);
  },
  unlisten(name, handler) {
    const win = this.win;
    if (!win) return;
    win.removeEventListener(name, handler);
  },
};

const MERMAID_PATH = "/public/vendor/mermaid/mermaid.min.js";
const SWITCH_EVENT = "boba:canvas";
// сигнал слежения с сервера: window_message -> postMessage -> message.
// Содержимое в сигнале не едет — компонент сам запрашивает нужное и
// обновляет состояние на месте, поэтому панель не пересоздаётся.
const SIGNAL_TYPE = "boba:canvas";
// бюджет памяти окон текста: текущее окно плюс соседнее для бесшовного стыка
const WINDOW_BUDGET = 2;

// Единственный слот custom_js занят SSO-кнопкой, поэтому библиотеку тянет сам
// компонент: тег переиспользуется всеми диаграммами на странице.
function useMermaid(needed) {
  const [state, setState] = useState(() =>
    browser.global("mermaid") ? "ready" : "loading"
  );

  useEffect(() => {
    if (!needed || browser.global("mermaid")) {
      if (browser.global("mermaid")) setState("ready");
      return;
    }

    const rootPath =
      browser.find('meta[property="og:root_path"]')?.content || "";
    const src = rootPath.replace(/\/$/, "") + MERMAID_PATH;

    let script = browser.find(`script[src="${src}"]`);
    if (!script) {
      script = browser.create("script");
      script.src = src;
      script.async = true;
      browser.head()?.appendChild(script);
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

// Выгрузка страницы: сокет чата умирает вместе с ней, и действие ухода уже
// некому исполнить — сервер снимает слежение сам, увидев тред без живых
// сокетов. Флаг гасит запросы, которые иначе улетают в закрытую сессию.
const pageExit = {
  leaving: false,
  watched: false,
  watch() {
    if (this.watched) return;
    this.watched = true;
    browser.listen("pagehide", () => {
      this.leaving = true;
    });
  },
};

// Режим показа панели живёт вне React-дерева. Содержимое панели сменяемо:
// другой канал, другой файл, пуш элемента с сервера, — и любая такая смена
// пересобирает вьювер. Режим к вьюверу не относится: это состояние самой
// панели, поэтому он в модуле, а не в состоянии сцены. Снимают его только
// явные действия пользователя — «Закрыть» на весь экран и Escape.
const panelMode = {
  full: false,
  listeners: new Set(),
  set(value) {
    if (this.full === value) return;
    this.full = value;
    this.listeners.forEach((notify) => notify(value));
  },
  subscribe(notify) {
    this.listeners.add(notify);
    return () => this.listeners.delete(notify);
  },
};

// Режим панели в компоненте: значение и переключатель. Подписка держит в
// согласии все сцены, включая пересозданные после пуша с сервера.
function usePanelMode() {
  const [full, setFull] = useState(panelMode.full);

  useEffect(() => panelMode.subscribe(setFull), []);

  const change = (value) => panelMode.set(value);
  return [full, change];
}

// Показанный файл — тоже состояние панели, а не вьювера: панель всегда
// показывает конкретный файл, поэтому скачивание живёт в общей шапке и
// достаётся каждому вьюверу без его участия.
const panelFile = {
  file: null,
  listeners: new Set(),
  set(value) {
    const same =
      this.file === value ||
      (this.file && value && this.file.url === value.url);
    if (same) return;
    this.file = value;
    this.listeners.forEach((notify) => notify(value));
  },
  subscribe(notify) {
    this.listeners.add(notify);
    return () => this.listeners.delete(notify);
  },
};

function usePanelFile() {
  const [file, setFile] = useState(panelFile.file);

  useEffect(() => panelFile.subscribe(setFile), []);

  return file;
}


// Полноэкранный режим — тот же самый узел панели, поднятый в корень
// приложения: панель chainlit анимируется трансформацией, а внутри
// трансформированного предка position:fixed меряется от него, а не от окна.
// Портала у раннера нет (react-dom не отдаётся), поэтому узел переносится
// вручную — но именно в корень React, иначе делегированные onClick перестают
// его слышать. React рисует в узел детей и его места в DOM не касается, так
// что содержимое можно менять сколько угодно: оверлей это не трогает.
function useOverlay(nodeRef, full, onEscape) {
  useEffect(() => {
    if (!full) return;

    const node = nodeRef.current;
    const host = browser.overlayHost();
    if (!node || !host) return;

    const slot = browser.create("div");
    if (!slot) return;
    carryScroll(node, () => {
      node.parentNode.insertBefore(slot, node);
      host.appendChild(node);
    });

    const onKey = (event) => {
      if (event.key === "Escape") onEscape();
    };
    browser.listen("keydown", onKey);

    return () => {
      browser.unlisten("keydown", onKey);
      // узел обязан вернуться на место до размонтирования: иначе React
      // будет убирать его из прежнего родителя, где его уже нет
      carryScroll(node, () => {
        if (slot.parentNode) slot.parentNode.insertBefore(node, slot);
        slot.remove();
      });
    };
  }, [full]);
}

// перенос узла в DOM обнуляет прокрутку его потомков, поэтому позиция
// снимается до переезда и возвращается сразу после
function carryScroll(node, move) {
  const boxes = [...node.querySelectorAll("[data-canvas-scroll]")];
  const tops = boxes.map((box) => box.scrollTop);

  move();

  boxes.forEach((box, index) => {
    // прокрутка ставится только на посчитанную раскладку: у только что
    // перенесённого узла высота ещё нулевая, и присваивание пропадает
    void box.scrollHeight;
    box.scrollTop = tops[index];
  });
}

// ——— Единый набор управления для всех типов канваса ———
// Один стиль кнопки, один ряд-тулбар, одна сцена: панельный и полноэкранный
// показ — один и тот же DOM-узел, поэтому приход данных и смена содержимого
// не сворачивают полный экран и не сбрасывают состояние вьювера.

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

// Единая шапка сцены: ряд кнопок вьювера, затем замыкающие. В панели —
// «на весь экран» и закрытие панели; на весь экран закрытие сворачивает
// панель обратно. Клики по шапке не всплывают в карточку.
function StageBar({ preview, status, children }) {
  const [full, setFull] = usePanelMode();
  const file = usePanelFile();
  const stop = (event) => event.stopPropagation();

  const closePanel = () => {
    const back = browser.find("#side-view-title button");
    if (back) back.click();
  };

  // скачивание — тем же роутом отдачи файлов: сервер отдаёт его потоком,
  // в память целиком не поднимая
  const download = () => {
    if (!file) return;
    const link = browser.create("a");
    if (!link) return;
    link.href = file.url;
    link.download = file.name;
    browser.body()?.appendChild(link);
    link.click();
    link.remove();
  };

  let save = null;
  if (!preview && file) {
    save = (
      <ToolButton onClick={download} title="Download file">
        <Download />
      </ToolButton>
    );
  }

  let trail = null;
  if (!preview && full) {
    trail = (
      <ToolButton onClick={() => setFull(false)} title="Close">
        <X />
      </ToolButton>
    );
  }
  if (!preview && !full) {
    trail = (
      <>
        <ToolButton onClick={() => setFull(true)} title="Fullscreen">
          <Maximize2 />
        </ToolButton>
        <ToolButton onClick={closePanel} title="Close">
          <X />
        </ToolButton>
      </>
    );
  }

  return (
    <div
      className="flex-shrink-0 flex items-center gap-1 py-4 px-6 border-b border-border bg-card"
      onClick={stop}
      onPointerDown={stop}
    >
      <div
        data-canvas-status=""
        className="flex-1 min-w-0 truncate text-[11px] text-muted-foreground/80"
      >
        {status}
      </div>
      {children}
      {save}
      {trail}
    </div>
  );
}

// Сцена: шапка сверху, тело занимает остаток и прокручивается само. Оверлей
// полноэкранного режима сцене не принадлежит — его держит панель, поэтому
// смена вьювера или содержимого режим не трогает.
function Stage({ status, bar, preview, children }) {
  const [full] = usePanelMode();

  let shown = full;
  if (preview) shown = false;

  return (
    <div
      data-canvas-stage=""
      className="relative flex flex-col w-full overflow-hidden flex-1 min-h-0 h-full"
    >
      <StageBar preview={preview} status={status}>
        {bar}
      </StageBar>
      {children(shown)}
    </div>
  );
}

// Пан/зум-сцена для визуального содержимого (svg диаграммы, картинка).
// В панели колесо листает страницу, на весь экран — зумит.
function ZoomStage({ controls, status, preview, children }) {
  const [view, setView] = useState({ k: 1, x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const fullRef = useRef(false);
  const boxRef = useRef(null);
  const dragRef = useRef(null);

  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    const onWheel = (e) => {
      if (!fullRef.current) return;
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
  }, []);

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
    <Stage status={status} bar={bar} preview={preview}>
      {(full) => {
        fullRef.current = full;
        return (
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
        );
      }}
    </Stage>
  );
}

// Скролл-сцена для текстового содержимого (окно журнала, markdown): размер
// шрифта теми же кнопками. stick держит низ при доливе строк — прокрутка
// правится до кадра (useLayoutEffect), поэтому новая строка не дёргает
// вьюпорт. onEdge зовётся у кромок для догрузки соседнего окна.
function ScrollStage({
  stick,
  deps,
  render,
  controls,
  status,
  preview,
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
    if (!stick) return;
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
    <Stage status={status} bar={bar} preview={preview}>
      {() => (
        <div
          ref={boxRef}
          data-canvas-scroll=""
          onScroll={onScroll}
          className="flex-1 min-h-0 overflow-auto p-3"
        >
          {render(fontPx)}
        </div>
      )}
    </Stage>
  );
}

// Родное содержимое (pdf/видео/аудио) со своими контролами: над ним только
// общая шапка с «на весь экран» / «закрыть».
function NativeStage({ preview, children }) {
  return (
    <Stage preview={preview} bar={null}>
      {() => children}
    </Stage>
  );
}

function Diagram({ content, signal, preview }) {
  const [spec, setSpec] = useState(content.text || "");
  const [svg, setSvg] = useState("");
  const [error, setError] = useState(null);
  const [dark, setDark] = useState(() =>
    browser.root()?.classList.contains("dark") || false
  );
  const idRef = useRef(`mmd-${Math.random().toString(36).slice(2)}`);
  const hostRef = useRef(null);
  const reportedRef = useRef("");
  const library = useMermaid(true);

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

  // смена файла в открытой панели: компонент не размонтируется — состояние
  // прошлой спеки сбрасывается явно
  useEffect(() => {
    setSpec(content.text || "");
    setError(null);
    setSvg("");
  }, [content.path, content.nonce]);

  // сигнал слежения: файл переписан — забираем свежую спеку и рисуем на месте;
  // refresh не перерегистрирует слежение — оно остаётся за этим показом
  useEffect(() => {
    if (!signal) return;
    let cancelled = false;
    callAction({
      name: "canvas_content",
      payload: { path: content.path, refresh: true },
    }).then((answer) => {
      const next = answer && answer.response;
      if (cancelled || !next || typeof next.text !== "string") return;
      setSpec(next.text);
    });
    return () => {
      cancelled = true;
    };
  }, [signal && signal.revision]);

  useEffect(() => {
    const root = browser.root();
    if (!root) return undefined;
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
    browser.global("mermaid").initialize({
      startOnLoad: false,
      securityLevel: "strict",
      suppressErrorRendering: true,
      theme: dark ? "dark" : "default",
    });
    // третий аргумент — контейнер в DOM: mindmap/timeline меряют разметку
    // при рендере и падают во временном неприкреплённом элементе
    browser
      .global("mermaid")
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

  // аварийная и загрузочная ветки живут в той же сцене: ряд кнопок панели
  // (зум, полный экран, закрытие) не пропадает ни в одном состоянии
  let body = (
    <div
      className="w-full h-full"
      // единственное исключение: сюда попадает только вывод mermaid.render
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
  if (error) {
    body = (
      <div className="px-2 py-1 flex flex-col gap-2 overflow-auto h-full">
        <div className="text-xs text-muted-foreground">
          diagram not rendered: {error}
        </div>
        <pre className="overflow-auto text-xs font-mono">{spec}</pre>
      </div>
    );
  } else if (!svg) {
    body = (
      <div className="px-2 py-1 text-xs text-muted-foreground">
        rendering the diagram…
      </div>
    );
  }

  return (
    <div className="flex-1 min-h-0 flex flex-col relative">
      <div
        ref={hostRef}
        aria-hidden="true"
        className="absolute overflow-hidden"
        style={{ left: -10000, top: 0, width: 1200, height: 900 }}
      />
      <ZoomStage preview={preview}>{body}</ZoomStage>
    </div>
  );
}

// Ссылка с меткой ревизии: сигнал слежения меняет query — браузер сам
// перечитывает файл, компонент не пересоздаётся.
function revisedUrl(url, signal) {
  if (!url || !signal || !signal.revision) return url;
  const glue = url.includes("?") ? "&" : "?";
  return `${url}${glue}rev=${encodeURIComponent(signal.revision)}`;
}

function ImageView({ content, signal, preview }) {
  return (
    <ZoomStage preview={preview}>
      <img
        src={revisedUrl(content.url, signal)}
        alt={content.label}
        draggable={false}
        className="w-full h-full object-contain pointer-events-none"
      />
    </ZoomStage>
  );
}

// Вызов и канал из псевдо-пути показа stream://{call_id}/{channel}: разбор
// в одном месте, как и на сервере.
const streamPath = {
  SCHEME: "stream://",
  parse(path) {
    if (!path || !path.startsWith(this.SCHEME)) return null;
    const [callId, channel] = path.slice(this.SCHEME.length).split("/");
    if (!callId || !channel) return null;
    return { callId, channel };
  },
};

// Имя для сохранения: у файла workspace — его собственное, у журнала вызова
// оно собирается из частей псевдо-пути показа.
function fileOf(content) {
  if (!content || !content.url) return null;

  const parsed = streamPath.parse(content.path || "");
  if (parsed) {
    return { url: content.url, name: `${parsed.callId}.${parsed.channel}.log` };
  }

  const name = (content.path || "").split("/").pop() || content.label || "file";
  return { url: content.url, name };
}


// Экранные имена каналов. Наружу сервер отдаёт только stdout и stderr тела
// инструмента; служебные каналы вызова в панель не приходят.
const CHANNEL_LABEL = {
  tool_stdout: "stdout",
  tool_stderr: "stderr",
};

// Вкладки каналов вызова: каждый канал — свой файл журнала, поэтому
// переключение берёт у сервера описание другого файла того же вызова.
// Описание приходит ответом и подменяется на месте (inline): пуш элемента с
// сервера перемонтировал бы боковую панель целиком — с анимацией открытия и
// потерей состояния вьювера.
function ChannelTabs({ content, onContent }) {
  const channels = content.channels || [];
  if (channels.length < 2) return null;

  const parsed = streamPath.parse(content.path);
  if (!parsed) return null;

  const show = async (channel) => {
    if (channel === content.channel) return;
    const answer = await callAction({
      name: "canvas_stream",
      payload: { call_id: parsed.callId, channel, inline: true },
    });
    const next = answer && answer.response;
    if (next && next.kind) onContent(next);
  };

  const tabs = channels.map((channel) => {
    const active = channel === content.channel;
    let variant = "ghost";
    if (active) variant = "secondary";

    let label = CHANNEL_LABEL[channel];
    if (!label) label = channel;

    return (
      <Button
        key={channel}
        variant={variant}
        size="sm"
        className="h-6 px-2 text-[11px]"
        onClick={() => show(channel)}
        title={channel}
        aria-label={`Channel ${label}`}
        aria-pressed={active}
        data-canvas-channel={channel}
        data-active={active ? "true" : "false"}
      >
        {label}
      </Button>
    );
  });

  return <div className="flex items-center gap-1 mr-1">{tabs}</div>;
}

// Окно журнала или текстового файла, как less: непрерывная прокрутка окнами,
// файла в DOM целиком нет — в памяти не больше WINDOW_BUDGET окон, лишнее
// освобождается при смещении. Данные тянутся только когда пользователь сам
// стоит на конце файла (follow): сигнал слежения тогда доливает хвост; при
// прокрутке вверх сигналы обновляют лишь строку статуса, не трогая DOM окна.
function WindowedText({ content, signal, preview, onContent }) {
  const pos0 = content.stream || {
    offset: 0, end: 0, size: 0, window: 65536, closed: true,
  };
  const seeded = () => ({
    segments: [{ offset: pos0.offset, end: pos0.end, text: content.text || "" }],
    size: pos0.size,
    closed: pos0.closed,
    note: content.note || "",
  });
  const [doc, setDoc] = useState(seeded);

  const boxRef = useRef(null);
  const busyRef = useRef(false);
  const anchorRef = useRef(null);
  const jumpRef = useRef(null);
  const docRef = useRef(doc);
  docRef.current = doc;

  // низ окна по фактической раскладке, а не по тому, скроллил ли пользователь:
  // содержимое короче окна прокрутки не даёт вовсе, и живой вывод замирал бы
  const atBottom = () => {
    const box = boxRef.current;
    if (!box) return true;

    return box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  };

  // смена файла в открытой панели: компонент не размонтируется — цепочка
  // окон прошлого файла сбрасывается явно
  useEffect(() => {
    anchorRef.current = null;
    jumpRef.current = "top";
    setDoc(seeded());
  }, [content.path, content.nonce]);

  const first = doc.segments[0];
  const last = doc.segments[doc.segments.length - 1];
  const atEnd = last.end >= doc.size;

  const fetchWindow = async (bounds) => {
    const answer = await callAction({
      name: "canvas_stream_window",
      payload: { path: content.path, ...bounds },
    });
    const next = answer && answer.response;
    if (!next || !next.stream) return null;
    return next;
  };

  const segOf = (piece) => ({
    offset: piece.stream.offset,
    end: piece.stream.end,
    text: piece.text || "",
  });

  const metaOf = (piece) => ({
    size: piece.stream.size,
    closed: piece.stream.closed,
    note: piece.note || "",
  });

  // байты, а не символы: смещения окна считаются в байтах файла
  const bytesOf = (text) => new TextEncoder().encode(text).length;

  // живой вывод прирастает мелкими кусками — они сливаются в хвостовой
  // сегмент, а он подрезается спереди по бюджету и по границе строки
  const grownTail = (tail, piece) => {
    const merged = {
      offset: tail.offset,
      end: piece.stream.end,
      text: tail.text + (piece.text || ""),
    };

    const budget = WINDOW_BUDGET * (pos0.window || 65536);
    if (bytesOf(merged.text) <= budget) return merged;

    const excess = bytesOf(merged.text) - budget;
    const cut = merged.text.indexOf("\n", excess);
    if (cut < 0) return merged;

    merged.text = merged.text.slice(cut + 1);
    merged.offset = merged.end - bytesOf(merged.text);
    return merged;
  };

  const replaceAll = (piece, jump) => {
    jumpRef.current = jump;
    setDoc({ segments: [segOf(piece)], ...metaOf(piece) });
  };

  const loadBefore = async () => {
    const head = docRef.current.segments[0];
    if (head.offset <= 0) return;

    busyRef.current = true;
    try {
      const piece = await fetchWindow({ before: head.offset });
      if (!piece) return;
      const box = boxRef.current;
      anchorRef.current = box ? box.scrollHeight - box.scrollTop : null;
      setDoc((cur) => ({
        segments: [segOf(piece), ...cur.segments],
        ...metaOf(piece),
      }));
    } finally {
      busyRef.current = false;
    }
  };

  const loadAfter = async () => {
    const cur = docRef.current;
    const tail = cur.segments[cur.segments.length - 1];

    busyRef.current = true;
    try {
      const piece = await fetchWindow({ offset: tail.end });
      if (!piece || piece.stream.end <= tail.end) return;
      setDoc((prev) => ({
        segments: [...prev.segments, segOf(piece)],
        ...metaOf(piece),
      }));
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

  // сигнал слежения: у конца — долить хвост; выше — обновить только статус
  useEffect(() => {
    if (!signal) return;

    const cur = docRef.current;
    const tail = cur.segments[cur.segments.length - 1];

    // догонять хвост, пока пользователь стоит у нижней кромки: окно могло
    // отстать от файла, и требовать «уже в конце» значило залипнуть навсегда
    if (!atBottom()) {
      setDoc((prev) => ({
        ...prev,
        size: signal.size,
        closed: signal.closed,
        note: signal.note || "",
      }));
      return;
    }

    if (busyRef.current) return;
    busyRef.current = true;
    fetchWindow({ offset: tail.end })
      .then((piece) => {
        if (!piece) return;
        if (piece.stream.end <= tail.end) {
          setDoc((prev) => ({ ...prev, ...metaOf(piece) }));
          return;
        }
        setDoc((prev) => {
          const last = prev.segments[prev.segments.length - 1];
          const grown = grownTail(last, piece);
          return {
            segments: [...prev.segments.slice(0, -1), grown],
            ...metaOf(piece),
          };
        });
      })
      .finally(() => {
        busyRef.current = false;
      });
  }, [signal && signal.revision]);

  // компенсация prepend: контент вырос сверху, позиция держится якорем от низа
  useLayoutEffect(() => {
    const box = boxRef.current;
    const anchor = anchorRef.current;
    if (!box || anchor == null) return;
    anchorRef.current = null;
    box.scrollTop = box.scrollHeight - anchor;
  }, [doc.segments]);

  // подрезка до бюджета отдельным тиком: обрезка снизу позицию не трогает,
  // обрезка сверху компенсируется тем же якорем от низа
  useEffect(() => {
    if (doc.segments.length <= WINDOW_BUDGET) return;
    const box = boxRef.current;
    const nearTop = box && box.scrollTop < box.clientHeight;
    if (nearTop) {
      setDoc((cur) => ({
        ...cur,
        segments: cur.segments.slice(0, WINDOW_BUDGET),
      }));
      return;
    }
    anchorRef.current = box ? box.scrollHeight - box.scrollTop : null;
    setDoc((cur) => ({
      ...cur,
      segments: cur.segments.slice(-WINDOW_BUDGET),
    }));
  }, [doc.segments]);

  // «в начало» и «в конец» — локальные запросы окон: панель не пересоздаётся
  useLayoutEffect(() => {
    const jump = jumpRef.current;
    if (!jump) return;
    jumpRef.current = null;
    const box = boxRef.current;
    if (!box) return;
    if (jump === "top") {
      box.scrollTop = 0;
      return;
    }
    box.scrollTop = box.scrollHeight;
  }, [doc.segments]);

  const toStart = async () => {
    const piece = await fetchWindow({ offset: 0 });
    if (piece) replaceAll(piece, "top");
  };
  const toEnd = async () => {
    const piece = await fetchWindow({ offset: -1 });
    if (piece) replaceAll(piece, "bottom");
  };

  // скачивание — файловым роутом; сервер отдаёт файл потоком, не в память
  const controls = (
    <>
      <ChannelTabs content={content} onContent={onContent} />
      <ToolButton onClick={toStart} title="Go to the file start">
        <ArrowUpToLine />
      </ToolButton>
      <ToolButton onClick={toEnd} title="Go to the file end and follow output">
        <ArrowDownToLine />
      </ToolButton>
    </>
  );

  const text = doc.segments.map((segment) => segment.text).join("");

  let state = doc.note;
  if (!doc.closed && !state) state = "running…";
  const status = `${first.offset}–${last.end} / ${doc.size} ${state}`.trim();

  // прилипание к низу: окно у конца файла и пользователь его не покидал
  const following = atEnd;

  return (
    <ScrollStage
      stick={following}
      deps={[doc.segments]}
      boxRef={boxRef}
      onEdge={onEdge}
      controls={controls}
      status={status}
      preview={preview}
      render={(fontPx) => (
        <pre
          className="font-mono whitespace-pre-wrap break-words"
          style={{ fontSize: fontPx }}
        >
          {text}
        </pre>
      )}
    />
  );
}

// Markdown: формат требует документ целиком — файл читается по ссылке заново
// при каждом сигнале слежения, но сама панель не пересоздаётся.
function MarkdownFile({ content, signal, preview }) {
  const [text, setText] = useState("");
  const [failed, setFailed] = useState(false);
  const url = revisedUrl(content.url, signal);

  useEffect(() => {
    let cancelled = false;
    setFailed(false);
    fetch(url)
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
  }, [url]);

  if (failed) {
    return (
      <div className="p-3 text-xs text-muted-foreground">
        file is not readable: {content.label}
      </div>
    );
  }

  return (
    <ScrollStage
      deps={[text]}
      preview={preview}
      render={(fontPx) => (
        <div style={{ fontSize: fontPx }}>
          <Markdown allowHtml={false} latex={false}>
            {text}
          </Markdown>
        </div>
      )}
    />
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

// Объяснение вместо содержимого — в той же сцене: ряд кнопок панели
// (полный экран, закрытие) не пропадает и здесь.
function Notice({ content, preview }) {
  return (
    <Stage preview={preview} bar={null}>
      {() => (
        <div className="flex-1 min-h-0 flex items-center justify-center p-6">
          <div className="max-w-md text-center text-sm text-muted-foreground">
            {content.note}
          </div>
        </div>
      )}
    </Stage>
  );
}

function Body({ content, signal, preview, onContent }) {
  switch (content.kind) {
    case "mermaid":
      return <Diagram content={content} signal={signal} preview={preview} />;
    case "image":
      return <ImageView content={content} signal={signal} preview={preview} />;
    case "pdf":
      return (
        <NativeStage preview={preview}>
          <iframe
            src={revisedUrl(content.url, signal)}
            title={content.label}
            className="flex-1 min-h-0 w-full border-0"
          />
        </NativeStage>
      );
    case "video":
      return (
        <NativeStage preview={preview}>
          <div className="flex-1 min-h-0 flex items-center justify-center p-2">
            <video
              src={revisedUrl(content.url, signal)}
              controls
              className="max-w-full max-h-full"
            />
          </div>
        </NativeStage>
      );
    case "audio":
      return (
        <NativeStage preview={preview}>
          <div className="p-3">
            <audio
              src={revisedUrl(content.url, signal)}
              controls
              className="w-full"
            />
          </div>
        </NativeStage>
      );
    case "text":
      return <MarkdownFile content={content} signal={signal} preview={preview} />;
    case "stream":
      return (
        <WindowedText
          content={content}
          signal={signal}
          preview={preview}
          onContent={onContent}
        />
      );
    default:
      return <Notice content={content} preview={preview} />;
  }
}

// Единственный компонент панели: рисует любое содержимое и сам переключает
// файлы. После маунта сервер содержимое не пушит — обновления приходят
// сигналами слежения (window_message), и компонент сам тянет нужное окно
// или файл, поэтому панель не переоткрывается и DOM не пересоздаётся.
export default function CanvasView() {
  const [content, setContent] = useState(props);
  const [signal, setSignal] = useState(null);
  const [full, setFull] = usePanelMode();
  const contentRef = useRef(content);
  const panelRef = useRef(null);
  contentRef.current = content;

  // оверлей держит панель: её узел переживает и смену содержимого, и
  // пересоздание элемента сервером — режим восстанавливается из panelMode
  useOverlay(panelRef, full && !props.preview, () => setFull(false));

  useEffect(() => setContent(props), [props.path, props.nonce]);

  // показанный файл — общее состояние панели: шапка берёт его оттуда и рисует
  // скачивание одинаково для любого вьювера
  useEffect(() => {
    if (props.preview) return;

    panelFile.set(fileOf(content));
    return () => panelFile.set(null);
  }, [props.preview, content.path, content.url]);

  // показ, добытый действием (вкладка канала): сигнал прежнего показа
  // устарел вместе с ним, слежение сервер переставил сам
  const switchContent = (next) => {
    setSignal(null);
    setContent(next);
  };

  // сигналы слежения с сервера: chainlit пробрасывает window_message в
  // window.postMessage — фильтруем свой тип и путь показанного файла
  useEffect(() => {
    if (props.preview) return;

    const onMessage = (event) => {
      const data = event.data;
      if (!data || data.type !== SIGNAL_TYPE) return;
      if (data.path !== contentRef.current.path) return;
      setSignal(data);
    };

    browser.listen("message", onMessage);
    return () => browser.unlisten("message", onMessage);
  }, [props.preview]);

  // уход с показа (закрытие панели, смена файла, пересоздание элемента):
  // сервер снимает слежение этого nonce; чужой nonce он не тронет. Уход
  // вместе со страницей — не наш случай: сессии уже нет, и слежение гаснет
  // само, когда у треда не остаётся живых сокетов
  useEffect(() => {
    if (props.preview) return;

    pageExit.watch();

    const shown = { path: content.path, nonce: content.nonce || "" };
    return () => {
      if (pageExit.leaving) return;

      callAction({ name: "canvas_leave", payload: shown });
    };
  }, [content.path, content.nonce, props.preview]);

  // карточка в ленте: тот же рендер, но кликом показывает файл в панели
  const openInCanvas = () => {
    if (browser.byId("side-view-content")) {
      browser.emit(
        new CustomEvent(SWITCH_EVENT, { detail: { path: content.path } })
      );
      return;
    }

    callAction({ name: "canvas_open", payload: { path: content.path } });
  };

  // смена файла в открытой панели: описание берётся действием, элемент
  // не подменяется — иначе chainlit пересоздал бы панель с анимацией
  useEffect(() => {
    if (props.preview) return;

    const onSwitch = (event) => {
      const path = event.detail && event.detail.path;
      if (!path || path === contentRef.current.path) return;

      callAction({ name: "canvas_content", payload: { path } }).then((answer) => {
        const next = answer && answer.response;
        if (next && next.kind) {
          setSignal(null);
          setContent(next);
        }
      });
    };

    browser.listen(SWITCH_EVENT, onSwitch);
    return () => browser.unlisten(SWITCH_EVENT, onSwitch);
  }, [props.preview]);

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
            <Body content={content} signal={null} preview={true} />
          </div>
        </div>
      </div>
    );
  }

  const panel =
    "border border-border rounded-lg bg-card overflow-hidden w-full " +
    "flex-1 min-h-0 h-full flex flex-col relative";
  const overlay = "flex flex-col bg-card overflow-hidden";

  let shell = panel;
  if (full) shell = overlay;

  let stretch;
  if (full) stretch = { position: "fixed", inset: 0, zIndex: 50 };

  return (
    <div
      ref={panelRef}
      data-canvas-panel=""
      data-full={full ? "true" : "false"}
      className={shell}
      style={stretch}
    >
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
      <Body
        content={content}
        signal={signal}
        preview={false}
        onContent={switchContent}
      />
    </div>
  );
}
