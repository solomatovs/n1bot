import { Plus } from "lucide-react";
import { type ReactElement, useCallback, useState, useEffect } from "react";

import { useServices } from "../../app";
import { Async } from "../Async";
import { useLoadable } from "../../hooks/useLoadable";
import type { ConnectionView } from "../../model/account";
import { SchemaDoc, parseSchema } from "../../model/schema";
import { ConnectionForm } from "./ConnectionForm";

type Pick = { kind: "none" } | { kind: "new" } | { kind: "row"; id: string };

/** Вкладка соединений: свои — правятся, общие (по роли) — только просмотр. */
export function ConnectionsTab(): ReactElement {
  const { api, socket } = useServices();
  const [rows, reload] = useLoadable(
    useCallback(async () => {
      const [schema, list] = await Promise.all([api.connectionSchema(), api.connections()]);
      return { doc: new SchemaDoc(parseSchema(schema)), list };
    }, [api]),
  );
  const [pick, setPick] = useState<Pick>({ kind: "none" });

  // соединение, изменённое в другой вкладке или на другом инстансе, приходит по шине
  useEffect(
    () =>
      socket.onUser((event) => {
        if (event.kind === "connections_changed") {
          reload();
        }
      }),
    [socket, reload],
  );

  const renderRows = ({ doc, list: loaded }: { doc: SchemaDoc; list: ConnectionView[] }): ReactElement => {
    const mine = loaded.filter((row) => row.mine);
    const shared = loaded.filter((row) => !row.mine);
    const current = pick.kind === "row" ? (loaded.find((row) => row.id === pick.id) ?? null) : null;

    const item = (row: ConnectionView): ReactElement => (
      <button
        type="button"
        key={row.id}
        className={`item${current?.id === row.id ? " item--on" : ""}`}
        onClick={() => {
          setPick({ kind: "row", id: row.id });
        }}
      >
        <span className="item__name">{row.name}</span>
        <span className="item__meta">{row.kind}</span>
      </button>
    );

    return (
      <div className="connections">
        <div className="connections__list">
          <button
            type="button"
            className="list__new"
            onClick={() => {
              setPick({ kind: "new" });
            }}
          >
            <Plus size={14} />
            New connection
          </button>
          <div className="list__group">
            mine <span className="list__group-count">{mine.length}</span>
          </div>
          {mine.map(item)}
          <div className="list__group">
            shared <span className="list__group-count">{shared.length}</span>
          </div>
          {shared.map(item)}
        </div>
        <div className="connections__scene">
          {pick.kind === "none" && <div className="empty">Pick a connection or create a new one</div>}
          {pick.kind === "new" && (
            <ConnectionForm
              key="new"
              doc={doc}
              row={null}
              onSaved={(saved) => {
                reload();
                setPick({ kind: "row", id: saved.id });
              }}
              onRemoved={() => {
                reload();
                setPick({ kind: "none" });
              }}
            />
          )}
          {pick.kind === "row" && current !== null && (
            <ConnectionForm
              key={current.id}
              doc={doc}
              row={current}
              onSaved={() => {
                reload();
              }}
              onRemoved={() => {
                reload();
                setPick({ kind: "none" });
              }}
            />
          )}
        </div>
      </div>
    );
  };

  return <Async state={rows} render={renderRows} />;
}
