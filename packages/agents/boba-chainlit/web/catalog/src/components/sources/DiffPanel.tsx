import type { ReactElement } from "react";

import type { SourceDiff } from "../../model/catalog";
import { Chip, Eyebrow } from "../../ui";

type Props = {
  diff: SourceDiff;
  title: string;
};

/** Разница версий источника: объекты added/removed/modified, у изменённых —
 * поля «было → стало» и части (колонки, ограничения, индексы, аргументы). */
export function DiffPanel({ diff, title }: Props): ReactElement {
  return (
    <div className="detail" data-testid="source-diff" data-entries={diff.entries.length}>
      <header className="detail__head">
        <div className="detail__title">
          <Eyebrow>diff</Eyebrow>
          <h2 className="detail__name">{title}</h2>
        </div>
        <Chip tone="muted">{diff.entries.length} changes</Chip>
      </header>
      {diff.entries.length === 0 && <p className="detail__empty">nothing changed</p>}
      <ul className="diff">
        {diff.entries.map((entry) => (
          <li
            key={`${entry.ref.kind}:${entry.ref.path.join("/")}`}
            className="diff__entry"
            data-testid="diff-entry"
            data-status={entry.status}
            data-path={entry.ref.path.join("/")}
          >
            <div className="diff__head">
              <Chip tone="draft">{entry.status}</Chip>
              <Chip tone="muted">{entry.ref.kind}</Chip>
              <span className="mono">{entry.ref.path.join("/")}</span>
            </div>
            {entry.fields.length > 0 && (
              <ul className="diff__fields">
                {entry.fields.map((field) => (
                  <li key={field.field} className="mono" data-field={field.field}>
                    {field.field}: {field.was ?? "∅"} → {field.now ?? "∅"}
                  </li>
                ))}
              </ul>
            )}
            {entry.parts.length > 0 && (
              <ul className="diff__parts">
                {entry.parts.map((part) => (
                  <li key={`${part.part}:${part.name}`} data-part={part.part} data-name={part.name} data-status={part.status}>
                    <span className="mono">
                      {part.part} {part.name}
                    </span>
                    <Chip tone="draft">{part.status}</Chip>
                    {part.fields.length > 0 && (
                      <ul className="diff__fields">
                        {part.fields.map((field) => (
                          <li key={field.field} className="mono" data-field={field.field}>
                            {field.field}: {field.was ?? "∅"} → {field.now ?? "∅"}
                          </li>
                        ))}
                      </ul>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
