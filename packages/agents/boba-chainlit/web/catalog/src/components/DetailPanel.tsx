import { ArrowLeft, ArrowRight, KeyRound, X } from "lucide-react";
import type { ReactElement } from "react";

import type { Catalog, Dataset, Flow } from "../model/catalog";
import { Chip, Eyebrow, IconButton } from "../ui";

type Props = {
  catalog: Catalog;
  dataset: Dataset;
  showDiff: boolean;
  onActivate: (datasetId: string) => void;
  onClose: () => void;
};

/** Панель набора: паспорт, колонки, потоки в обе стороны с правилом загрузки.
 * Перенос TableDetail и RelatedTables из liam erd-core под каталог. */
export function DetailPanel({ catalog, dataset, showDiff, onActivate, onClose }: Props): ReactElement {
  const columns = catalog.columnsOf(dataset.id);
  const flows = catalog.flowsOf(dataset.id);
  const status = catalog.statusOf("dataset", dataset.id);
  const layer = catalog.layer(dataset.layer_id);

  return (
    <div className="detail" data-testid="detail-panel" data-dataset={dataset.name}>
      <header className="detail__head">
        <div className="detail__title">
          <Eyebrow>{layer?.name ?? "—"}</Eyebrow>
          <h2 className="detail__name">{dataset.name}</h2>
        </div>
        {showDiff && status !== "unchanged" && <Chip tone="draft">{status}</Chip>}
        <IconButton aria-label="close details" onClick={onClose}>
          <X size={16} />
        </IconButton>
      </header>

      <section className="detail__section">
        <dl className="detail__facts">
          <dt>source</dt>
          <dd>{dataset.source === "" ? "—" : dataset.source}</dd>
          <dt>owner</dt>
          <dd>{dataset.owner === "" ? "—" : dataset.owner}</dd>
          <dt>tags</dt>
          <dd>
            {dataset.tags.length === 0 ? "—" : dataset.tags.map((tag) => <Chip key={tag} tone="muted">{tag}</Chip>)}
          </dd>
        </dl>
        {dataset.description !== "" && <p className="detail__description">{dataset.description}</p>}
      </section>

      <section className="detail__section" data-testid="detail-columns">
        <Eyebrow as="h4">columns · {columns.length}</Eyebrow>
        <table className="detail__table">
          <tbody>
            {columns.map((column) => (
              <tr
                key={column.id}
                id={`dataset__${dataset.id}__column__${column.id}`}
                data-status={showDiff ? catalog.statusOf("column", column.id) : "unchanged"}
              >
                <td className="detail__icon">{column.is_key && <KeyRound size={11} />}</td>
                <td className="detail__col-name">{column.name}</td>
                <td className="detail__col-type">{column.type}</td>
                <td className="detail__col-null">{column.nullable ? "null" : "not null"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <FlowList
        title="incoming"
        icon={<ArrowLeft size={12} />}
        flows={flows.incoming}
        catalog={catalog}
        other={(flow) => flow.from_dataset_id}
        showDiff={showDiff}
        onActivate={onActivate}
      />
      <FlowList
        title="outgoing"
        icon={<ArrowRight size={12} />}
        flows={flows.outgoing}
        catalog={catalog}
        other={(flow) => flow.to_dataset_id}
        showDiff={showDiff}
        onActivate={onActivate}
      />
    </div>
  );
}

type FlowListProps = {
  title: string;
  icon: ReactElement;
  flows: Flow[];
  catalog: Catalog;
  other: (flow: Flow) => string;
  showDiff: boolean;
  onActivate: (datasetId: string) => void;
};

function FlowList({ title, icon, flows, catalog, other, showDiff, onActivate }: FlowListProps): ReactElement {
  return (
    <section className="detail__section" data-testid={`detail-${title}`}>
      <Eyebrow as="h4">
        {title} · {flows.length}
      </Eyebrow>
      {flows.length === 0 && <p className="detail__empty">none</p>}
      <ul className="detail__flows">
        {flows.map((flow) => {
          const otherId = other(flow);
          const neighbour = catalog.dataset(otherId);
          const status = showDiff ? catalog.statusOf("flow", flow.id) : "unchanged";
          return (
            <li key={flow.id} className="detail__flow" data-status={status}>
              <button
                type="button"
                className="detail__flow-target"
                onClick={() => {
                  onActivate(otherId);
                }}
              >
                {icon}
                <span>{neighbour?.name ?? otherId}</span>
              </button>
              <Chip>{catalog.loadKindName(flow)}</Chip>
              <dl className="detail__values">
                {catalog.loadValues(flow).map((value) => (
                  <div key={value.field} className="detail__value">
                    <dt>{value.field}</dt>
                    <dd>{value.text}</dd>
                  </div>
                ))}
              </dl>
              {flow.description !== "" && <p className="detail__description">{flow.description}</p>}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
