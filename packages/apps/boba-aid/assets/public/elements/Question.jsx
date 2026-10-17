import { useEffect, useRef, useState } from "react";
import { Markdown } from "@/components/markdown";
import { useTranslation } from "@/components/i18n";

// Вопрос агента пользователю. Висящий вопрос (state "pending") рисуется
// панелью у поля ввода (docked — её ставит Footer), а в ленте — строкой
// «Question» с мигающей точкой. Ответ — submitElement({action, content}) в
// ask chainlit. Записанный ответ (accept, decline, cancel) — та же строка с
// точкой исхода, строка статуса и вопросы с выбором только для чтения.
// Подписи — из переводов chainlit (components.Question), тексты вопросов и
// вариантов — как пришли.
const TEXT = "components.Question";
const OTHER_PICK = "\u0000other";
const LOCK_MS = 500;
const CONFIRM_MS = 300;
const HOVER_MS = 150;
const STATES = ["accept", "decline", "cancel"];
const CHOICES = ["choice", "multi", "boolean"];
const NUMBERS = ["number", "integer"];

function ChevronDown() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path
        fillRule="evenodd"
        d="M5.22 8.22a.75.75 0 0 1 1.06 0L10 11.94l3.72-3.72a.75.75 0 1 1 1.06 1.06l-4.25 4.25a.75.75 0 0 1-1.06 0L5.22 9.28a.75.75 0 0 1 0-1.06Z"
        clipRule="evenodd"
      />
    </svg>
  );
}

function ChevronUp() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path
        fillRule="evenodd"
        d="M9.47 6.47a.75.75 0 0 1 1.06 0l4.25 4.25a.75.75 0 1 1-1.06 1.06L10 8.06l-3.72 3.72a.75.75 0 0 1-1.06-1.06l4.25-4.25Z"
        clipRule="evenodd"
      />
    </svg>
  );
}

function Cross() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z" />
    </svg>
  );
}

function Check() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
      <path
        d="M2 6L5 9L10 3"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// Классы узла: базовые и те, чей признак истинен.
function cls(base, flags) {
  const names = [];
  if (base) names.push(base);
  for (const [name, on] of Object.entries(flags)) {
    if (on) names.push(name);
  }
  return names.join(" ");
}

// Подписи панели и записи на языке переводов chainlit.
function useLabels() {
  const { t } = useTranslation();
  const plain = { interpolation: { escapeValue: false } };
  return {
    other: t(`${TEXT}.other`),
    yes: t(`${TEXT}.yes`),
    no: t(`${TEXT}.no`),
    typed: t(`${TEXT}.typed`),
    submit: t(`${TEXT}.submit`),
    cancelHint: t(`${TEXT}.cancelHint`),
    noPreview: t(`${TEXT}.noPreview`),
    collapse: t(`${TEXT}.collapse`),
    expand: t(`${TEXT}.expand`),
    close: t(`${TEXT}.close`),
    preview: (caption) => t(`${TEXT}.preview`, { ...plain, caption }),
    title: (count) => {
      if (count === 1) return t(`${TEXT}.title.one`);
      return t(`${TEXT}.title.many`);
    },
    status: (state) => {
      if (STATES.includes(state)) return t(`${TEXT}.status.${state}`);
      return t(`${TEXT}.status.cancel`);
    },
    count: (count) => t(`${TEXT}.questions`, { count }),
  };
}

// Варианты вопроса: логическое поле — да/нет из переводов.
function optionsOf(item, labels) {
  if (item.kind !== "boolean") return item.options || [];
  return [
    { value: "true", label: labels.yes, description: "", preview: "" },
    { value: "false", label: labels.no, description: "", preview: "" },
  ];
}

function rowsOf(item, labels) {
  const rows = [];
  for (const option of optionsOf(item, labels)) {
    rows.push({ value: option.value, label: option.label, description: option.description, preview: option.preview });
  }
  if (item.other) rows.push({ value: OTHER_PICK, label: labels.other, description: "", preview: "" });
  return rows;
}

function numberOf(item, text) {
  const trimmed = text.trim();
  if (trimmed === "") return undefined;
  const value = Number(trimmed);
  if (!Number.isFinite(value)) return undefined;
  if (item.kind === "integer" && !Number.isInteger(value)) return undefined;
  if (item.minimum !== null && item.minimum !== undefined && value < item.minimum) return undefined;
  if (item.maximum !== null && item.maximum !== undefined && value > item.maximum) return undefined;
  return value;
}

function hasAnswer(item, picks, text) {
  if (item.kind === "text") return text.trim() !== "";
  if (NUMBERS.includes(item.kind)) return numberOf(item, text) !== undefined;
  return picks.length > 0;
}

function settled(item, picks, text) {
  if (hasAnswer(item, picks, text)) return true;
  if (item.required || item.other) return false;
  if (NUMBERS.includes(item.kind)) return text.trim() === "";
  return true;
}

function contentOf(items, picked, typed) {
  const content = {};
  for (const item of items) {
    const picks = picked[item.key] || [];
    const text = typed[item.key] || "";
    const chosen = picks.filter((value) => value !== OTHER_PICK);
    if (item.kind === "choice" && chosen.length) content[item.key] = chosen[0];
    if (item.kind === "multi" && chosen.length) content[item.key] = chosen;
    if (item.kind === "boolean" && chosen.length) content[item.key] = chosen[0] === "true";
    if (item.kind === "text" && text.trim() !== "") content[item.key] = text;
    if (NUMBERS.includes(item.kind)) {
      const value = numberOf(item, text);
      if (value !== undefined) content[item.key] = value;
    }
    if (item.other && picks.includes(OTHER_PICK)) content[item.other] = text;
  }
  return content;
}

function withPreview(item) {
  if (item.kind !== "choice") return false;
  return (item.options || []).some((option) => option.preview && option.preview.trim() !== "");
}

function previewLines(item) {
  let lines = 3;
  for (const option of item.options || []) {
    if (!option.preview) continue;
    lines = Math.max(lines, option.preview.replace(/\n+$/, "").split("\n").length + 1);
  }
  return lines;
}

function OptionRow({ row, index, multi, selected, readOnly, dimmed, highlighted, confirming, onPick, onKey, onFocusRow, onHover, onLeave, children }) {
  const classes = cls("option", {
    optionConfirming: confirming,
    optionReadOnly: readOnly,
    optionDimmed: dimmed,
    optionHighlighted: highlighted,
  });

  let dot = null;
  if (selected) dot = <div className="radioDot" />;
  let mark = <div className={cls("radio", { radioChecked: selected })}>{dot}</div>;
  let role = "radio";
  if (multi) {
    let tick = null;
    if (selected) tick = <Check />;
    mark = <div className={cls("checkbox", { checkboxChecked: selected })}>{tick}</div>;
    role = "checkbox";
  }

  let description = null;
  if (row.description) description = <div className="optionDescription">{row.description}</div>;

  const interactive = {};
  if (readOnly) interactive["aria-disabled"] = "true";
  if (!readOnly) {
    interactive.tabIndex = 0;
    interactive.onClick = () => onPick(row.value);
    interactive.onKeyDown = (event) => onKey(event, row.value);
    interactive.onFocus = () => onFocusRow(index);
    interactive.onMouseEnter = () => onHover(index);
    interactive.onMouseLeave = onLeave;
  }

  return (
    <div className={classes} role={role} aria-checked={Boolean(selected)} data-row={index} {...interactive}>
      <div className="optionCheckbox">{mark}</div>
      <div className="optionContent">
        <div className="optionLabel">{row.label}</div>
        {description}
        {children}
      </div>
    </div>
  );
}

function TypedBox({ value, onChange, onKey, inputRef, labels }) {
  const [focused, setFocused] = useState(false);
  let placeholder = (
    <div className="q-placeholder" aria-hidden="true">
      {labels.typed}
    </div>
  );
  if (value) placeholder = null;
  return (
    <div
      className={cls("q-wrapper otherInput", { "is-focus-within": focused })}
      onClick={(event) => event.stopPropagation()}
    >
      {placeholder}
      <div
        ref={inputRef}
        className="q-input"
        contentEditable="plaintext-only"
        role="textbox"
        aria-multiline="true"
        aria-label={labels.typed}
        spellCheck={false}
        suppressContentEditableWarning
        onInput={(event) => onChange(event.currentTarget.textContent || "")}
        onKeyDown={onKey}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
      />
    </div>
  );
}

function PreviewPane({ item, row, labels }) {
  const option = (item.options || [])[row];
  let caption = labels.other;
  if (option) caption = option.label;
  let body = <div className="previewEmpty">{labels.noPreview}</div>;
  if (option && option.preview && option.preview.trim() !== "") {
    const text = option.preview.replace(/\n+$/, "");
    body = (
      <div className="previewBody">
        <pre>{text}</pre>
      </div>
    );
    if (text.includes("```")) {
      body = (
        <div className="previewBody previewMarkdown">
          <Markdown allowHtml={false} latex={false}>
            {text}
          </Markdown>
        </div>
      );
    }
  }
  return (
    <div
      className="previewPane"
      role="region"
      aria-label={labels.preview(caption)}
      tabIndex={-1}
      style={{ "--preview-lines": previewLines(item) }}
    >
      <div className="previewCaption" aria-hidden="true">
        {caption}
      </div>
      {body}
    </div>
  );
}

function Panel({ card }) {
  const labels = useLabels();
  const items = card.items;
  const [active, setActive] = useState(0);
  const [picked, setPicked] = useState({});
  const [typed, setTyped] = useState({});
  const [highlight, setHighlight] = useState(null);
  const [confirming, setConfirming] = useState(null);
  const [folded, setFolded] = useState(false);
  const [locked, setLocked] = useState(true);
  const [focusWithin, setFocusWithin] = useState(true);
  const [focusTarget, setFocusTarget] = useState({ kind: "row", index: 0 });
  const [overflow, setOverflow] = useState({ top: false, bottom: false });
  const rootRef = useRef(null);
  const questionsRef = useRef(null);
  const typedRef = useRef(null);
  const submitRef = useRef(null);
  const hoverTimer = useRef(undefined);
  const confirmTimer = useRef(undefined);
  const doneRef = useRef(false);

  const item = items[active];
  const rows = rowsOf(item, labels);
  const picks = picked[item.key] || [];
  const text = typed[item.key] || "";
  const preview = withPreview(item) && !folded;
  const allSettled = items.every((each) => settled(each, picked[each.key] || [], typed[each.key] || ""));

  useEffect(() => {
    const timer = setTimeout(() => {
      setLocked(false);
      const ready = items.every((each) => settled(each, picked[each.key] || [], typed[each.key] || ""));
      if (ready) setFocusTarget({ kind: "submit" });
    }, LOCK_MS);
    return () => clearTimeout(timer);
  }, []);

  useEffect(() => () => {
    clearTimeout(hoverTimer.current);
    clearTimeout(confirmTimer.current);
  }, []);

  useEffect(() => {
    const root = rootRef.current;
    if (!root || !focusTarget) return;
    if (focusTarget.kind === "submit" && submitRef.current) {
      submitRef.current.focus();
      return;
    }
    if (focusTarget.kind === "typed" && typedRef.current) {
      const box = typedRef.current;
      box.focus();
      const range = document.createRange();
      range.selectNodeContents(box);
      range.collapse(false);
      const selection = window.getSelection();
      if (selection) {
        selection.removeAllRanges();
        selection.addRange(range);
      }
      return;
    }
    const found = root.querySelectorAll('[role="radio"], [role="checkbox"]');
    if (found.length && focusTarget.kind === "row") {
      const index = Math.min(focusTarget.index, found.length - 1);
      found[index].focus({ preventScroll: focusTarget.quiet === true });
      return;
    }
    if (!found.length && typedRef.current) typedRef.current.focus();
  }, [focusTarget, active]);

  useEffect(() => {
    const box = typedRef.current;
    if (!box) return;
    if ((box.textContent || "") !== text) box.textContent = text;
  }, [active, picks.includes(OTHER_PICK)]);

  useEffect(() => {
    const box = questionsRef.current;
    if (!box) return;
    const measure = () => {
      setOverflow({
        top: box.scrollTop > 0,
        bottom: box.scrollTop + box.clientHeight < box.scrollHeight - 1,
      });
    };
    measure();
    box.addEventListener("scroll", measure);
    return () => box.removeEventListener("scroll", measure);
  }, [active, folded, picked]);

  const finish = (payload) => {
    if (doneRef.current) return;
    doneRef.current = true;
    submitElement(payload);
  };

  const submit = (nextPicked, nextTyped) => {
    if (locked) return;
    const usePicked = nextPicked || picked;
    const useTyped = nextTyped || typed;
    const ready = items.every((each) => settled(each, usePicked[each.key] || [], useTyped[each.key] || ""));
    if (!ready) return;
    finish({ action: "accept", content: contentOf(items, usePicked, useTyped) });
  };

  const decline = () => {
    if (locked) return;
    finish({ action: "decline" });
  };

  const goTo = (index) => {
    setActive(index);
    setConfirming(null);
    setFocusTarget({ kind: "row", index: 0 });
  };

  const pick = (value) => {
    const multi = item.kind === "multi";
    const current = picked[item.key] || [];
    let next = [value];
    if (multi) next = [...current, value];
    if (multi && current.includes(value)) next = current.filter((each) => each !== value);
    const nextPicked = { ...picked, [item.key]: next };
    setPicked(nextPicked);

    const opened = value === OTHER_PICK && next.includes(OTHER_PICK);
    if (opened) {
      setFocusTarget({ kind: "typed" });
      return nextPicked;
    }

    const index = rows.findIndex((row) => row.value === value);
    setFocusTarget({ kind: "row", index, quiet: true });
    const last = active >= items.length - 1;
    if (!multi && confirming === null && !last) {
      setConfirming(value);
      confirmTimer.current = setTimeout(() => {
        setConfirming(null);
        setActive((current) => Math.min(current + 1, items.length - 1));
        setFocusTarget({ kind: "row", index: 0 });
      }, CONFIRM_MS);
    }
    return nextPicked;
  };

  const onRowKey = (event, value) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    event.stopPropagation();
    if (event.repeat) return;
    const answeredBefore = allSettled;
    const nextPicked = pick(value);
    if (event.key === "Enter" && answeredBefore) submit(nextPicked, typed);
  };

  const setHighlightRow = (index) => setHighlight({ question: active, row: index });

  const onFocusRow = (index) => {
    if (!withPreview(item)) return;
    clearTimeout(hoverTimer.current);
    setHighlightRow(index);
  };

  const onHover = (index) => {
    if (!withPreview(item)) return;
    clearTimeout(hoverTimer.current);
    hoverTimer.current = setTimeout(() => setHighlightRow(index), HOVER_MS);
  };

  const onLeave = () => clearTimeout(hoverTimer.current);

  const onTyped = (value) => setTyped({ ...typed, [item.key]: value });

  const onTypedKey = (event) => {
    if (!event.metaKey && !event.ctrlKey) event.stopPropagation();
    if (event.key !== "Enter" || event.shiftKey) return;
    if (event.nativeEvent.isComposing) return;
    event.preventDefault();
    if (active < items.length - 1) goTo(active + 1);
  };

  const onKeysDown = (event) => {
    if (folded) return;
    if (event.key === "ArrowLeft" && active > 0) {
      event.stopPropagation();
      goTo(active - 1);
      return;
    }
    if (event.key === "ArrowRight" && active < items.length - 1) {
      event.stopPropagation();
      goTo(active + 1);
      return;
    }
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    const found = Array.from(rootRef.current.querySelectorAll('[role="radio"], [role="checkbox"]'));
    if (!found.length) return;
    const at = found.findIndex((row) => row === document.activeElement);
    let next = at + 1;
    if (at >= found.length - 1) next = 0;
    if (event.key === "ArrowUp") next = at - 1;
    if (event.key === "ArrowUp" && at <= 0) next = found.length - 1;
    event.preventDefault();
    event.stopPropagation();
    setFocusTarget({ kind: "row", index: next });
  };

  const onContainerKey = (event) => {
    const inText = event.target.isContentEditable === true;
    const guarded = event.key === "Enter" || event.key === "Escape" || /^[0-9]$/.test(event.key);
    if (event.repeat && !inText && guarded) {
      event.preventDefault();
      return;
    }
    if (folded) {
      if (event.key === "Escape") event.preventDefault();
      return;
    }
    if (event.target === submitRef.current && event.key === "Enter") return;
    if (locked) return;
    if (event.key === "1") {
      event.preventDefault();
      submit();
      return;
    }
    if (event.key === "Enter" && !event.metaKey && !event.ctrlKey) {
      event.preventDefault();
      submit();
      return;
    }
    if (event.key === "Escape" && !event.metaKey && !event.ctrlKey) {
      event.preventDefault();
      decline();
    }
  };

  let highlighted = -1;
  if (preview) {
    highlighted = 0;
    if (highlight !== null && highlight.question === active) highlighted = highlight.row;
  }

  const tabs = items.map((each, index) => {
    const classes = ["navTab"];
    if (index === active) classes.push("navTabActive");
    if (hasAnswer(each, picked[each.key] || [], typed[each.key] || "")) classes.push("navTabAnswered");
    return (
      <button
        key={each.key}
        type="button"
        className={classes.join(" ")}
        data-tab={index}
        onClick={() => {
          setFolded(false);
          goTo(index);
        }}
      >
        <span className="navTabLabel">{each.header}</span>
      </button>
    );
  });

  const typedBox = (
    <TypedBox value={text} onChange={onTyped} onKey={onTypedKey} inputRef={typedRef} labels={labels} />
  );

  const optionRows = rows.map((row, index) => {
    const selected = picks.includes(row.value);
    let children = null;
    if (row.value === OTHER_PICK && selected) children = <div data-other-wrap>{typedBox}</div>;
    return (
      <OptionRow
        key={row.value}
        row={row}
        index={index}
        multi={item.kind === "multi"}
        selected={selected}
        highlighted={highlighted === index}
        confirming={confirming === row.value}
        onPick={pick}
        onKey={onRowKey}
        onFocusRow={onFocusRow}
        onHover={onHover}
        onLeave={onLeave}
      >
        {children}
      </OptionRow>
    );
  });

  const questionClasses = cls("questionsContainer", {
    questionsContainerFolded: folded,
    questionsContainerOverflowTop: overflow.top,
    questionsContainerOverflowBottom: overflow.bottom,
  });
  const containerClasses = cls("permissionRequestContainer", {
    folded,
    "is-focus-within": focusWithin,
  });

  let source = null;
  if (card.source) {
    source = (
      <div className="q-source">
        <b>{card.source.server}</b> · {card.source.message}
      </div>
    );
  }

  let fold = { label: labels.collapse, icon: <ChevronDown /> };
  if (folded) fold = { label: labels.expand, icon: <ChevronUp /> };

  let answers = <div className="optionsContainer">{typedBox}</div>;
  if (CHOICES.includes(item.kind)) answers = <div className="optionsContainer">{optionRows}</div>;

  let pane = null;
  if (preview) pane = <PreviewPane item={item} row={highlighted} labels={labels} />;

  return (
    <div className="q-root q-dock" data-question-panel>
      <div
        ref={rootRef}
        className={containerClasses}
        tabIndex={0}
        data-focused-index="0"
        onKeyDown={onContainerKey}
        onFocus={() => setFocusWithin(true)}
        onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) setFocusWithin(false);
        }}
      >
        <div className="permissionRequestContainerBackground" />
        <div className="permissionRequestContent">
          {source}
          <div className={cls("", { withPreview: preview })} onKeyDown={onKeysDown}>
            <div className={cls("navigationBar", { navigationBarFolded: folded })}>
              {tabs}
              <div className="navSpacer" />
              <button
                type="button"
                className="iconButton iconButton20"
                data-fold
                aria-label={fold.label}
                aria-expanded={!folded}
                onClick={() => setFolded(!folded)}
              >
                {fold.icon}
              </button>
              <button type="button" className="iconButton iconButton20" data-close aria-label={labels.close} onClick={decline}>
                <Cross />
              </button>
            </div>
            <div ref={questionsRef} className={questionClasses}>
              <div className="questionBlock">
                <div className="questionHeader">
                  <span className="questionTextLarge">{item.text}</span>
                </div>
                {answers}
              </div>
            </div>
            {pane}
          </div>
        </div>
        <div className="buttonContainer">
          <button
            ref={submitRef}
            type="button"
            className="q-button"
            data-submit
            disabled={locked || !allSettled}
            onClick={() => submit()}
          >
            <span className="shortcutNum">1</span> {labels.submit}
          </button>
        </div>
        <div className="keyboardHints">{labels.cancelHint}</div>
      </div>
    </div>
  );
}

function ReviewRows({ item, answer, state, labels }) {
  let picks = [];
  let typedText = null;
  if (answer) {
    picks = answer.picked || [];
    typedText = answer.typed;
  }
  const typed = typedText !== null && typedText !== undefined;
  const any = picks.length > 0 || typed;
  const noop = () => undefined;

  if (!CHOICES.includes(item.kind)) {
    if (!typed) return null;
    return <div className="optionDescription typedAnswer q-typedOnly">{typedText}</div>;
  }

  const rows = optionsOf(item, labels).map((option, index) => {
    const selected = picks.includes(option.value);
    return (
      <OptionRow
        key={option.value}
        row={option}
        index={index}
        multi={item.kind === "multi"}
        selected={selected}
        readOnly
        dimmed={state === "accept" && any && !selected}
        onPick={noop}
        onKey={noop}
        onFocusRow={noop}
        onHover={noop}
        onLeave={noop}
      />
    );
  });

  if (typed) {
    let text = null;
    if (typedText) text = <div className="optionDescription typedAnswer">{typedText}</div>;
    rows.push(
      <OptionRow
        key={OTHER_PICK}
        row={{ value: OTHER_PICK, label: labels.other, description: "" }}
        index={rows.length}
        multi={item.kind === "multi"}
        selected
        readOnly
        onPick={noop}
        onKey={noop}
        onFocusRow={noop}
        onHover={noop}
        onLeave={noop}
      >
        {text}
      </OptionRow>
    );
  }

  return <div className="optionsContainer">{rows}</div>;
}

function Heading({ card, tone }) {
  const labels = useLabels();
  return (
    <div className="q-heading" data-question-heading={tone}>
      <span className={cls("q-dot", { [`q-dot-${tone}`]: true })} aria-hidden="true" />
      <span className="toolNameText">{labels.title(card.items.length)}</span>
    </div>
  );
}

function Waiting({ card }) {
  return (
    <div className="q-root q-record" data-question-waiting="">
      <Heading card={card} tone="progress" />
    </div>
  );
}

function Review({ card }) {
  const labels = useLabels();
  const [open, setOpen] = useState(true);
  let tone = "success";
  if (card.failed) tone = "failure";
  const toggle = () => setOpen(!open);

  let source = null;
  if (card.source) {
    source = (
      <div className="q-source">
        <b>{card.source.server}</b> · {card.source.message}
      </div>
    );
  }

  let chevron = <ChevronDown />;
  let body = null;
  if (open) {
    chevron = <ChevronUp />;
    body = (
      <div className="toolBody">
        <div className="reviewBody">
          {source}
          {card.items.map((item) => (
            <div className="questionBlock" key={item.key}>
              <div className="questionHeader">
                <span className="questionTextLarge">{item.text}</span>
              </div>
              <ReviewRows item={item} answer={card.answers[item.key]} state={card.state} labels={labels} />
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="q-root q-record q-review" data-question-review={card.state}>
      <Heading card={card} tone={tone} />
      <div
        className="secondaryLine"
        role="button"
        tabIndex={0}
        aria-expanded={open}
        onClick={toggle}
        onKeyDown={(event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          toggle();
        }}
      >
        <span className="q-content">
          <span className="statusLine">
            {labels.status(card.state)} · {labels.count(card.items.length)}
            {chevron}
          </span>
        </span>
      </div>
      {body}
    </div>
  );
}

export default function Question() {
  const card = props;
  if (card.state === "pending") {
    if (!card.docked) return <Waiting card={card} />;
    return <Panel card={card} />;
  }
  return <Review card={card} />;
}
