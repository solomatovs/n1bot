import { useEffect, useRef, useState } from "react";
import { Markdown } from "@/components/markdown";

// Вопрос агента пользователю. Висящий вопрос (state "pending") рисуется
// только панелью у поля ввода (docked — её ставит Footer), копия в шаге
// ленты пуста. Ответ — submitElement({action, content}) в ask chainlit.
// Записанный ответ (accept, decline, cancel) — строка статуса и вопросы с
// выбором только для чтения.
const OTHER = "Other";
const OTHER_PICK = "\u0000other";
const LOCK_MS = 500;
const CONFIRM_MS = 300;
const HOVER_MS = 150;
const STATUS = { accept: "Answered", decline: "Declined", cancel: "No answer" };
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

function rowsOf(item) {
  const rows = [];
  for (const option of item.options || []) {
    rows.push({ value: option.value, label: option.label, description: option.description, preview: option.preview });
  }
  if (item.other) rows.push({ value: OTHER_PICK, label: OTHER, description: "", preview: "" });
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
  const classes = ["option"];
  if (confirming) classes.push("optionConfirming");
  if (readOnly) classes.push("optionReadOnly");
  if (dimmed) classes.push("optionDimmed");
  if (highlighted) classes.push("optionHighlighted");

  let mark = (
    <div className={selected ? "radio radioChecked" : "radio"}>{selected ? <div className="radioDot" /> : null}</div>
  );
  if (multi) {
    mark = <div className={selected ? "checkbox checkboxChecked" : "checkbox"}>{selected ? <Check /> : null}</div>;
  }

  const interactive = {};
  if (!readOnly) {
    interactive.tabIndex = 0;
    interactive.onClick = () => onPick(row.value);
    interactive.onKeyDown = (event) => onKey(event, row.value);
    interactive.onFocus = () => onFocusRow(index);
    interactive.onMouseEnter = () => onHover(index);
    interactive.onMouseLeave = onLeave;
  }

  return (
    <div
      className={classes.join(" ")}
      role={multi ? "checkbox" : "radio"}
      aria-checked={Boolean(selected)}
      aria-disabled={readOnly ? "true" : undefined}
      data-row={index}
      {...interactive}
    >
      <div className="optionCheckbox">{mark}</div>
      <div className="optionContent">
        <div className="optionLabel">{row.label}</div>
        {row.description ? <div className="optionDescription">{row.description}</div> : null}
        {children}
      </div>
    </div>
  );
}

function TypedBox({ value, onChange, onKey, inputRef }) {
  const [focused, setFocused] = useState(false);
  return (
    <div
      className={focused ? "q-wrapper otherInput is-focus-within" : "q-wrapper otherInput"}
      onClick={(event) => event.stopPropagation()}
    >
      {value ? null : (
        <div className="q-placeholder" aria-hidden="true">
          Type your answer…
        </div>
      )}
      <div
        ref={inputRef}
        className="q-input"
        contentEditable="plaintext-only"
        role="textbox"
        aria-multiline="true"
        aria-label="Type your answer…"
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

function PreviewPane({ item, row }) {
  const option = (item.options || [])[row];
  let caption = OTHER;
  if (option) caption = option.label;
  let body = <div className="previewEmpty">No preview for this option</div>;
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
      aria-label={"Preview: " + caption}
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
  const rows = rowsOf(item);
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
    if (multi) {
      next = current.includes(value) ? current.filter((each) => each !== value) : [...current, value];
    }
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
    let next = at >= found.length - 1 ? 0 : at + 1;
    if (event.key === "ArrowUp") next = at <= 0 ? found.length - 1 : at - 1;
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
    <TypedBox value={text} onChange={onTyped} onKey={onTypedKey} inputRef={typedRef} />
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

  const questionClasses = ["questionsContainer"];
  if (folded) questionClasses.push("questionsContainerFolded");
  if (overflow.top) questionClasses.push("questionsContainerOverflowTop");
  if (overflow.bottom) questionClasses.push("questionsContainerOverflowBottom");

  const containerClasses = ["permissionRequestContainer"];
  if (folded) containerClasses.push("folded");
  if (focusWithin) containerClasses.push("is-focus-within");

  return (
    <div className="q-root q-dock" data-question-panel>
      <div
        ref={rootRef}
        className={containerClasses.join(" ")}
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
          {card.source ? (
            <div className="q-source">
              <b>{card.source.server}</b> · {card.source.message}
            </div>
          ) : null}
          <div className={preview ? "withPreview" : ""} onKeyDown={onKeysDown}>
            <div className={folded ? "navigationBar navigationBarFolded" : "navigationBar"}>
              {tabs}
              <div className="navSpacer" />
              <button
                type="button"
                className="iconButton iconButton20"
                data-fold
                aria-label={folded ? "Expand" : "Collapse"}
                aria-expanded={!folded}
                onClick={() => setFolded(!folded)}
              >
                {folded ? <ChevronUp /> : <ChevronDown />}
              </button>
              <button type="button" className="iconButton iconButton20" data-close aria-label="Close" onClick={decline}>
                <Cross />
              </button>
            </div>
            <div ref={questionsRef} className={questionClasses.join(" ")}>
              <div className="questionBlock">
                <div className="questionHeader">
                  <span className="questionTextLarge">{item.text}</span>
                </div>
                {CHOICES.includes(item.kind) ? (
                  <div className="optionsContainer">{optionRows}</div>
                ) : (
                  <div className="optionsContainer">{typedBox}</div>
                )}
              </div>
            </div>
            {preview ? <PreviewPane item={item} row={highlighted} /> : null}
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
            <span className="shortcutNum">1</span> Submit answers
          </button>
        </div>
        <div className="keyboardHints">Esc to cancel</div>
      </div>
    </div>
  );
}

function ReviewRows({ item, answer, state }) {
  const picks = (answer && answer.picked) || [];
  const typedText = answer ? answer.typed : null;
  const any = picks.length > 0 || (typedText !== null && typedText !== undefined);
  const noop = () => undefined;

  if (!CHOICES.includes(item.kind)) {
    if (typedText === null || typedText === undefined) return null;
    return <div className="optionDescription typedAnswer q-typedOnly">{typedText}</div>;
  }

  const rows = (item.options || []).map((option, index) => {
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

  if (typedText !== null && typedText !== undefined) {
    rows.push(
      <OptionRow
        key={OTHER_PICK}
        row={{ value: OTHER_PICK, label: OTHER, description: "" }}
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
        {typedText ? <div className="optionDescription typedAnswer">{typedText}</div> : null}
      </OptionRow>
    );
  }

  return <div className="optionsContainer">{rows}</div>;
}

function Review({ card }) {
  const [open, setOpen] = useState(true);
  const count = card.items.length;
  const noun = count === 1 ? "question" : "questions";
  const status = STATUS[card.state] || STATUS.cancel;
  const toggle = () => setOpen(!open);

  return (
    <div className="q-root q-review" data-question-review={card.state}>
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
            {status} · {count} {noun}
            {open ? <ChevronUp /> : <ChevronDown />}
          </span>
        </span>
      </div>
      {open ? (
        <div className="toolBody">
          <div className="reviewBody">
            {card.items.map((item) => (
              <div className="questionBlock" key={item.key}>
                <div className="questionHeader">
                  <span className="questionTextLarge">{item.text}</span>
                </div>
                <ReviewRows item={item} answer={card.answers[item.key]} state={card.state} />
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default function Question() {
  const card = props;
  if (card.state === "pending") {
    if (!card.docked) return null;
    return <Panel card={card} />;
  }
  return <Review card={card} />;
}
