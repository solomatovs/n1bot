import { Eye, EyeOff } from "lucide-react";
import { useMemo, useState, type ReactElement } from "react";

import type { Catalog, Dataset } from "../model/catalog";
import { Eyebrow, IconButton, Input } from "../ui";

type Props = {
  catalog: Catalog;
  datasets: Dataset[];
  activeId: string | undefined;
  hidden: ReadonlySet<string>;
  showDiff: boolean;
  onActivate: (datasetId: string) => void;
  onToggleHidden: (datasetId: string) => void;
};

/** Список наборов по слоям: поиск по имени, выбор, глаз скрывает набор на холсте.
 * Перенос LeftPane и useTableVisibility из liam erd-core. */
export function LeftPane({ catalog, datasets, activeId, hidden, showDiff, onActivate, onToggleHidden }: Props): ReactElement {
  const [query, setQuery] = useState("");

  const groups = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return catalog.layers
      .map((layer) => ({
        layer,
        datasets: datasets.filter(
          (dataset) => dataset.layer_id === layer.id && (needle === "" || dataset.name.toLowerCase().includes(needle)),
        ),
      }))
      .filter((group) => group.datasets.length > 0);
  }, [catalog, datasets, query]);

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
            <Eyebrow>{group.layer.name}</Eyebrow>
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
      </div>
    </div>
  );
}
