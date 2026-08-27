import type { ReactElement } from "react";

import { resultSummary } from "../../model/results";
import type { ToolResult } from "../../model/workflow";
import { JsonView } from "../JsonView";
import { StatusPill } from "../StatusPill";

/** Итог задачи в инспекторе: своя форма на каждый kind ToolResult,
 * неизвестный вид — деревом json. */

const MAX_ROWS = 50;

type Props = {
  result: ToolResult;
};

function Note({ text }: { text: string | null | undefined }): ReactElement | null {
  if (text === null || text === undefined || text === "") {
    return null;
  }

  return <div className="result__note">{text}</div>;
}

function Meta({ items }: { items: [string, string][] }): ReactElement {
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

function TableView({ rows, note }: { rows: Record<string, unknown>[]; note: string | null }): ReactElement {
  const columns = rows.length === 0 ? [] : Object.keys(rows[0] ?? {});
  const shown = rows.slice(0, MAX_ROWS);

  return (
    <div className="result result--table">
      <div className="result__scroll">
        <table className="table table--result">
          <thead>
            <tr>
              {columns.map((column) => (
                <th key={column}>{column}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((row, index) => (
              <tr key={index}>
                {columns.map((column) => (
                  <td key={column}>{cellText(row[column])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length > MAX_ROWS && (
        <div className="result__note">
          first {MAX_ROWS} of {rows.length} rows
        </div>
      )}
      {rows.length === 0 && <div className="result__note">no rows</div>}
      <Note text={note} />
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

function ShellView({ result }: { result: Extract<ToolResult, { kind: "shell" }> }): ReactElement {
  const exit = result.timed_out ? "timed out" : `exit ${result.exit_code}`;
  return (
    <div className="result result--shell" data-exit={result.exit_code}>
      <Meta items={[["status", exit], ["duration", `${result.duration_ms} ms`]]} />
      {result.stdout !== "" && (
        <>
          <div className="result__label">stdout{result.stdout_truncated ? " · truncated" : ""}</div>
          <pre className="inspector__code result__stream">{result.stdout}</pre>
        </>
      )}
      {result.stderr !== "" && (
        <>
          <div className="result__label">stderr{result.stderr_truncated ? " · truncated" : ""}</div>
          <pre className="inspector__code inspector__code--error result__stream">{result.stderr}</pre>
        </>
      )}
      <Note text={result.diagnostic} />
    </div>
  );
}

function ItemView({ result }: Props): ReactElement {
  switch (result.kind) {
    case "table":
      return <TableView rows={result.rows} note={result.note} />;
    case "shell":
      return <ShellView result={result} />;
    case "text":
      return (
        <div className="result result--text">
          {result.language !== "" && <div className="result__label">{result.language}</div>}
          <pre className="inspector__code">{result.text}</pre>
          <Note text={result.note} />
        </div>
      );
    case "affected":
      return (
        <div className="result result--affected">
          <Meta
            items={[
              ["affected rows", result.affected_rows === null ? "—" : String(result.affected_rows)],
              ["status", result.status ?? ""],
            ]}
          />
        </div>
      );
    case "json":
      return (
        <div className="result result--json inspector__code">
          <JsonView value={result.payload} clip={0} />
        </div>
      );
    case "chart":
      return (
        <div className="result result--chart">
          <Note text={result.title} />
          <div className="inspector__code">
            <JsonView value={result.spec} clip={0} />
          </div>
        </div>
      );
    case "diagram":
      return (
        <div className="result result--diagram">
          <Note text={result.title} />
          <pre className="inspector__code">{result.spec}</pre>
          <Meta items={[["path", result.path]]} />
        </div>
      );
    case "custom_element":
      return (
        <div className="result result--element">
          <Meta items={[["element", result.element]]} />
          <div className="inspector__code">
            <JsonView value={result.props} clip={0} />
          </div>
        </div>
      );
    case "multi":
      return (
        <div className="result result--multi">
          {result.items.map((item, index) => (
            <div className="result__item" key={index}>
              <div className="result__label">
                #{index + 1} · {item.kind}
              </div>
              <ItemView result={item} />
            </div>
          ))}
        </div>
      );
    case "error":
      return (
        <div className="result result--error">
          <pre className="inspector__code inspector__code--error">{result.message}</pre>
          <Meta items={[["kind", result.error_kind]]} />
        </div>
      );
    case "opaque":
      return (
        <div className="result inspector__code">
          <JsonView value={result.payload} clip={0} />
        </div>
      );
  }
}

export function ResultView({ result }: Props): ReactElement {
  const summary = resultSummary(result);
  return (
    <section className="result-view" data-kind={result.kind} aria-label="task result">
      <div className="result-view__head">
        <span className="chip">{summary.kind}</span>
        <span className="result-view__figure">{summary.figure}</span>
        <span className="result-view__detail">{summary.detail}</span>
        <span className="viewbar__spacer" />
        <StatusPill status={result.ok ? "done" : "failed"} />
      </div>
      <ItemView result={result} />
    </section>
  );
}
