import { Eye, EyeOff, Pencil, Plus, Trash2 } from "lucide-react";
import { useMemo, useState, type ReactElement } from "react";

import type { Catalog, Dataset } from "../model/catalog";
import type { EditActions } from "../model/editing";
import { Button, Eyebrow, IconButton, Input } from "../ui";

type Props = {
  catalog: Catalog;
  datasets: Dataset[];
  activeId: string | undefined;
  hidden: ReadonlySet<string>;
  showDiff: boolean;
  editing: EditActions | undefined;
  onActivate: (datasetId: string) => void;
  onToggleHidden: (datasetId: string) => void;
};

/** Список наборов по слоям: поиск по имени, выбор, глаз скрывает набор на холсте.
 * Перенос LeftPane и useTableVisibility из liam erd-core. */
export function LeftPane({
  catalog,
  datasets,
  activeId,
  hidden,
  showDiff,
  editing,
  onActivate,
  onToggleHidden,
}: Props): ReactElement {
  const [query, setQuery] = useState("");

  // в черновике пустые слои видны: в них добавляют наборы
  const groups = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return catalog.layers
      .map((layer) => ({
        layer,
        datasets: datasets.filter(
          (dataset) => dataset.layer_id === layer.id && (needle === "" || dataset.name.toLowerCase().includes(needle)),
        ),
      }))
      .filter((group) => group.datasets.length > 0 || (editing !== undefined && needle === ""));
  }, [catalog, datasets, query, editing]);

  return (
    <div className="pane" data-testid="left-pane">
      <div className="pane__search">
        <Input
          type="search"
          mono
          placeholder="find a dataset"
          aria-label="find a dataset"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
          }}
        />
      </div>
      <div className="pane__scroll">
        {groups.map((group) => (
          <section key={group.layer.id} className="pane__group" data-layer={group.layer.name}>
            <div className="pane__group-head">
              <Eyebrow>{group.layer.name}</Eyebrow>
              {editing !== undefined && (
                <span className="pane__group-actions">
                  <IconButton
                    className="pane__eye"
                    aria-label={`add dataset to ${group.layer.name}`}
                    onClick={() => {
                      editing.addDataset(group.layer.id);
                    }}
                  >
                    <Plus size={14} />
                  </IconButton>
                  <IconButton
                    className="pane__eye"
                    aria-label={`rename layer ${group.layer.name}`}
                    onClick={() => {
                      editing.renameLayer(group.layer);
                    }}
                  >
                    <Pencil size={12} />
                  </IconButton>
                  {group.datasets.length === 0 && (
                    <IconButton
                      className="pane__eye"
                      aria-label={`remove layer ${group.layer.name}`}
                      onClick={() => {
                        editing.removeLayer(group.layer);
                      }}
                    >
                      <Trash2 size={12} />
                    </IconButton>
                  )}
                </span>
              )}
            </div>
            <ul className="pane__list">
              {group.datasets.map((dataset) => {
                const status = showDiff ? catalog.statusOf("dataset", dataset.id) : "unchanged";
                return (
                  <li
                    key={dataset.id}
                    className="pane__item"
                    data-active={dataset.id === activeId}
                    data-hidden={hidden.has(dataset.id)}
                    data-status={status}
                    data-testid="pane-item"
                  >
                    <button
                      type="button"
                      className="pane__name"
                      onClick={() => {
                        onActivate(dataset.id);
                      }}
                    >
                      {dataset.name}
                    </button>
                    <IconButton
                      className="pane__eye"
                      aria-label={hidden.has(dataset.id) ? `show ${dataset.name}` : `hide ${dataset.name}`}
                      aria-pressed={hidden.has(dataset.id)}
                      onClick={() => {
                        onToggleHidden(dataset.id);
                      }}
                    >
                      {hidden.has(dataset.id) ? <EyeOff size={14} /> : <Eye size={14} />}
                    </IconButton>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
        {groups.length === 0 && <p className="pane__empty">nothing matches</p>}
        {editing !== undefined && (
          <div className="pane__footer">
            <Button size="tiny" icon={Plus} onClick={editing.addLayer}>
              layer
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
