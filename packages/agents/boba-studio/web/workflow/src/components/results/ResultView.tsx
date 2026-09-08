import type { ReactElement } from "react";

import type { TaskStatus } from "../../model/status";
import type { StudioBlock, TaskState } from "../../model/workflow";
import { JsonView } from "../JsonView";
import { Cell, Chip, Code, DataTable, Eyebrow, TableRow, Toolbar, ToolbarSpacer } from "../../ui";

/** Итог задачи в инспекторе: блоки словаря StudioView по порядку. Вид
 * результата страница не разбирает — показ собрал сам результат. */

const MAX_ROWS = 50;

type Props = {
  state: TaskState;
};

function Note({ text }: { text: string }): ReactElement | null {
  if (text === "") {
    return null;
  }

  return <div className="result__note">{text}</div>;
}

function Facts({ items }: { items: [string, string][] }): ReactElement {
  return (
    <dl className="result__meta">
      {items.map(([key, value]) => (
        <div className="result__fact" key={key}>
          <dt>{key}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function Grid({ rows }: { rows: Record<string, unknown>[] }): ReactElement {
  const columns = rows.length === 0 ? [] : Object.keys(rows[0] ?? {});
  const shown = rows.slice(0, MAX_ROWS);

  return (
    <div className="result result--table">
      <DataTable head={columns} mark="result-table">
        {shown.map((row, index) => (
          <TableRow key={index}>
            {columns.map((column) => (
              <Cell key={column}>{cellText(row[column])}</Cell>
            ))}
          </TableRow>
        ))}
      </DataTable>
      {rows.length > MAX_ROWS && (
        <div className="result__note">
          first {MAX_ROWS} of {rows.length} rows
        </div>
      )}
      {rows.length === 0 && <div className="result__note">no rows</div>}
    </div>
  );
}

function cellText(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }

  if (typeof value === "string") {
    return value;
  }

  return JSON.stringify(value);
}

function Block({ block, failed }: { block: StudioBlock; failed: boolean }): ReactElement {
  switch (block.block) {
    case "code":
      return (
        <div className="result result--text">
          {block.language !== "" && <Eyebrow>{block.language}</Eyebrow>}
          <Code inset tone={failed ? "error" : "default"}>
            {block.text}
          </Code>
        </div>
      );
    case "grid":
      return <Grid rows={block.rows} />;
    case "facts":
      return <Facts items={block.facts.map((fact) => [fact.key, fact.value])} />;
    case "note":
      return <Note text={block.text} />;
    case "widget":
      return (
        <div className="result result--element">
          <Facts items={[["element", block.element], ["title", block.title]]} />
          <Code inset>
            <JsonView value={block.props} clip={0} />
          </Code>
        </div>
      );
  }
}

function statusOf(ok: boolean): TaskStatus {
  if (ok) {
    return "done";
  }

  return "failed";
}

export function ResultView({ state }: Props): ReactElement | null {
  const result = state.result;
  const view = state.view;
  if (result === null || view === null) {
    return null;
  }

  return (
    <section className="result-view" data-kind={result.kind} aria-label="task result">
      <Toolbar>
        <Chip>{result.kind}</Chip>
        <span className="result-view__figure">{view.summary.figure}</span>
        <span className="result-view__detail">{view.summary.detail}</span>
        <ToolbarSpacer />
        <Chip status={statusOf(result.ok)} />
      </Toolbar>
      {view.blocks.map((block, index) => (
        <Block key={index} block={block} failed={!result.ok} />
      ))}
    </section>
  );
}
