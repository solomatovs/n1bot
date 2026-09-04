import { KeyRound } from "lucide-react";
import type { ReactElement, ReactNode } from "react";

import type { ObjectCard } from "../../model/catalog";
import { Chip, Eyebrow } from "../../ui";

type Props = {
  card: ObjectCard;
  /** Кнопки справа в шапке: правка и удаление на черновике ручного источника. */
  actions?: ReactNode;
};

/** Родная карточка объекта источника: у Postgres и ClickHouse свой набор
 * фактов и таблиц, ничего не приводится к общему виду. */
export function ObjectCardPanel({ card, actions }: Props): ReactElement {
  return (
    <div className="detail" data-testid="object-card" data-card={card.card} data-path={card.ref.path.join("/")}>
      {card.card === "pg_relation" && <PgRelationView card={card} actions={actions} />}
      {card.card === "pg_routine" && <PgRoutineView card={card} actions={actions} />}
      {card.card === "pg_sequence" && <PgSequenceView card={card} actions={actions} />}
      {card.card === "pg_type" && <PgTypeView card={card} actions={actions} />}
      {card.card === "ch_table" && <ChTableView card={card} actions={actions} />}
      {card.card === "ch_dictionary" && <ChDictionaryView card={card} actions={actions} />}
    </div>
  );
}

type Head = { eyebrow: string; name: string; kind: string; comment: string | null; actions: ReactNode };

function CardHead({ eyebrow, name, kind, comment, actions }: Head): ReactElement {
  return (
    <>
      <header className="detail__head">
        <div className="detail__title">
          <Eyebrow>{eyebrow}</Eyebrow>
          <h2 className="detail__name">{name}</h2>
        </div>
        <Chip tone="muted">{kind}</Chip>
        {actions}
      </header>
      {comment !== null && comment !== "" && (
        <p className="detail__description detail__description--head" data-testid="card-comment">
          {comment}
        </p>
      )}
    </>
  );
}

type Fact = { label: string; value: string | number | boolean | null | undefined };

function Facts({ facts, mark }: { facts: Fact[]; mark: string }): ReactElement {
  const shown = facts.filter((fact) => fact.value !== null && fact.value !== undefined && fact.value !== "");
  return (
    <dl className="detail__facts" data-testid={mark}>
      {shown.map((fact) => (
        <FactRow key={fact.label} fact={fact} />
      ))}
    </dl>
  );
}

function FactRow({ fact }: { fact: Fact }): ReactElement {
  return (
    <>
      <dt>{fact.label}</dt>
      <dd className="mono">{String(fact.value)}</dd>
    </>
  );
}

function Code({ title, text, mark }: { title: string; text: string; mark: string }): ReactElement {
  return (
    <section className="detail__section" data-testid={mark}>
      <Eyebrow as="h4">{title}</Eyebrow>
      <pre className="detail__code mono">{text}</pre>
    </section>
  );
}

function bytes(value: number | null): string {
  if (value === null) {
    return "";
  }

  if (value < 1024 * 1024) {
    return `${Math.round(value / 1024)} KiB`;
  }

  if (value < 1024 * 1024 * 1024) {
    return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
  }

  return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
}

type Viewer<T extends ObjectCard["card"]> = { card: Extract<ObjectCard, { card: T }>; actions: ReactNode };

function PgRelationView({ card, actions }: Viewer<"pg_relation">): ReactElement {
  const relation = card.relation;
  return (
    <>
      <CardHead
        eyebrow={`${relation.database} / ${relation.schema_name}`}
        name={relation.name}
        kind={relation.kind}
        comment={relation.comment}
        actions={actions}
      />
      <section className="detail__section">
        <Facts
          mark="card-facts"
          facts={[
            { label: "owner", value: relation.owner },
            { label: "rows", value: relation.row_estimate },
            { label: "size", value: bytes(relation.total_bytes) },
            { label: "persistence", value: relation.persistence === "permanent" ? "" : relation.persistence },
            { label: "tablespace", value: relation.tablespace },
            { label: "partition key", value: relation.partition_key },
            { label: "partition of", value: relation.partition_of },
            { label: "bound", value: relation.partition_bound },
            { label: "check option", value: relation.check_option },
            { label: "populated", value: relation.populated },
            { label: "foreign server", value: relation.foreign_server },
          ]}
        />
      </section>
      <section className="detail__section" data-testid="card-columns">
        <Eyebrow as="h4">columns · {card.columns.length}</Eyebrow>
        <table className="detail__table">
          <tbody>
            {card.columns.map((column) => (
              <tr key={column.name} data-column={column.name}>
                <td className="detail__icon">{isKey(card, column.name) && <KeyRound size={11} />}</td>
                <td className="detail__col-name">{column.name}</td>
                <td className="detail__col-type">{column.type}</td>
                <td className="detail__col-null">{column.nullable ? "null" : "not null"}</td>
                <td className="detail__col-extra">
                  {column.default !== null && <span>default {column.default}</span>}
                  {column.identity !== null && <span>identity {column.identity}</span>}
                  {column.generated !== null && <span>generated {column.generated}</span>}
                </td>
                <td className="detail__col-comment">{column.comment ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      {card.constraints.length > 0 && (
        <section className="detail__section" data-testid="card-constraints">
          <Eyebrow as="h4">constraints · {card.constraints.length}</Eyebrow>
          <ul className="detail__lines">
            {card.constraints.map((constraint) => (
              <li key={constraint.name} className="mono">
                <Chip tone="muted">{constraint.kind}</Chip> {constraint.name}: {constraint.definition}
              </li>
            ))}
          </ul>
        </section>
      )}
      {card.indexes.length > 0 && (
        <section className="detail__section" data-testid="card-indexes">
          <Eyebrow as="h4">indexes · {card.indexes.length}</Eyebrow>
          <ul className="detail__lines">
            {card.indexes.map((index) => (
              <li key={index.name} className="mono">
                {index.name}: {index.definition}
              </li>
            ))}
          </ul>
        </section>
      )}
      {card.partitions.length > 0 && (
        <section className="detail__section" data-testid="card-partitions">
          <Eyebrow as="h4">partitions · {card.partitions.length}</Eyebrow>
          <ul className="detail__lines">
            {card.partitions.map((partition) => (
              <li key={partition.name} className="mono">
                {partition.name} {partition.partition_bound ?? ""}
              </li>
            ))}
          </ul>
        </section>
      )}
      {relation.definition !== null && <Code title="definition" text={relation.definition} mark="card-definition" />}
    </>
  );
}

function isKey(card: Extract<ObjectCard, { card: "pg_relation" }>, column: string): boolean {
  for (const constraint of card.constraints) {
    if (constraint.kind !== "primary") {
      continue;
    }

    if (constraint.columns.includes(column)) {
      return true;
    }
  }

  return false;
}

function PgRoutineView({ card, actions }: Viewer<"pg_routine">): ReactElement {
  const routine = card.routine;
  return (
    <>
      <CardHead
        eyebrow={`${routine.database} / ${routine.schema_name}`}
        name={`${routine.name}(${routine.signature})`}
        kind={routine.kind}
        comment={routine.comment}
        actions={actions}
      />
      <section className="detail__section">
        <Facts
          mark="card-facts"
          facts={[
            { label: "language", value: routine.language },
            { label: "returns", value: routine.returns_set ? `setof ${routine.returns ?? ""}` : routine.returns },
            { label: "volatility", value: routine.volatility },
            { label: "strict", value: routine.strict },
            { label: "security definer", value: routine.security_definer ? true : "" },
            { label: "parallel", value: routine.parallel },
            { label: "owner", value: routine.owner },
          ]}
        />
      </section>
      <section className="detail__section" data-testid="card-arguments">
        <Eyebrow as="h4">arguments · {card.arguments.length}</Eyebrow>
        <table className="detail__table">
          <tbody>
            {card.arguments.map((argument) => (
              <tr key={argument.position}>
                <td className="detail__col-name">{argument.name ?? `$${argument.position + 1}`}</td>
                <td className="detail__col-type">{argument.type}</td>
                <td className="detail__col-null">{argument.mode}</td>
                <td className="detail__col-extra">{argument.default !== null && <span>default {argument.default}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <Code title="body" text={routine.body} mark="card-body" />
      {routine.definition !== "" && <Code title="definition" text={routine.definition} mark="card-definition" />}
    </>
  );
}

function PgSequenceView({ card, actions }: Viewer<"pg_sequence">): ReactElement {
  const sequence = card.sequence;
  return (
    <>
      <CardHead
        eyebrow={`${sequence.database} / ${sequence.schema_name}`}
        name={sequence.name}
        kind="sequence"
        comment={sequence.comment}
        actions={actions}
      />
      <section className="detail__section">
        <Facts
          mark="card-facts"
          facts={[
            { label: "type", value: sequence.type },
            { label: "start", value: sequence.start },
            { label: "increment", value: sequence.increment },
            { label: "min", value: sequence.minimum },
            { label: "max", value: sequence.maximum },
            { label: "cycle", value: sequence.cycle },
            { label: "cache", value: sequence.cache },
            { label: "last value", value: sequence.last_value },
            { label: "owned by", value: sequence.owned_by },
          ]}
        />
      </section>
    </>
  );
}

function PgTypeView({ card, actions }: Viewer<"pg_type">): ReactElement {
  const type = card.type;
  return (
    <>
      <CardHead
        eyebrow={`${type.database} / ${type.schema_name}`}
        name={type.name}
        kind={type.kind}
        comment={type.comment}
        actions={actions}
      />
      <section className="detail__section">
        <Facts
          mark="card-facts"
          facts={[
            { label: "owner", value: type.owner },
            { label: "labels", value: type.labels?.join(", ") },
            { label: "base type", value: type.base_type },
            { label: "constraint", value: type.constraint },
            { label: "attributes", value: type.attributes?.map((a) => `${a.name} ${a.type}`).join(", ") },
          ]}
        />
      </section>
    </>
  );
}

function ChTableView({ card, actions }: Viewer<"ch_table">): ReactElement {
  const table = card.table;
  return (
    <>
      <CardHead eyebrow={table.database} name={table.name} kind={table.kind} comment={table.comment} actions={actions} />
      <section className="detail__section">
        <Facts
          mark="card-facts"
          facts={[
            { label: "engine", value: table.engine_full === "" ? table.engine : table.engine_full },
            { label: "rows", value: table.total_rows },
            { label: "size", value: table.total_bytes === null ? "" : bytes(table.total_bytes) },
            { label: "partition by", value: table.partition_key },
            { label: "order by", value: table.sorting_key },
            { label: "primary key", value: table.primary_key },
            { label: "sample by", value: table.sampling_key },
            { label: "ttl", value: table.ttl },
            { label: "target", value: table.target },
            { label: "dependencies", value: table.dependencies.join(", ") },
            { label: "modified", value: table.metadata_modified_at },
          ]}
        />
      </section>
      <section className="detail__section" data-testid="card-columns">
        <Eyebrow as="h4">columns · {card.columns.length}</Eyebrow>
        <table className="detail__table">
          <tbody>
            {card.columns.map((column) => (
              <tr key={column.name} data-column={column.name}>
                <td className="detail__icon">{column.in_primary_key && <KeyRound size={11} />}</td>
                <td className="detail__col-name">{column.name}</td>
                <td className="detail__col-type">{column.type}</td>
                <td className="detail__col-extra">
                  {column.default_kind !== null && (
                    <span>
                      {column.default_kind} {column.default_expression ?? ""}
                    </span>
                  )}
                  {column.codec !== null && <span>codec {column.codec}</span>}
                  {column.ttl !== null && <span>ttl {column.ttl}</span>}
                </td>
                <td className="detail__col-comment">{column.comment ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      {table.definition !== null && <Code title="definition" text={table.definition} mark="card-definition" />}
      {table.create_query !== "" && <Code title="create query" text={table.create_query} mark="card-create-query" />}
    </>
  );
}

function ChDictionaryView({ card, actions }: Viewer<"ch_dictionary">): ReactElement {
  const dictionary = card.dictionary;
  return (
    <>
      <CardHead
        eyebrow={dictionary.database}
        name={dictionary.name}
        kind="dictionary"
        comment={dictionary.comment}
        actions={actions}
      />
      <section className="detail__section">
        <Facts
          mark="card-facts"
          facts={[
            { label: "status", value: dictionary.status },
            { label: "layout", value: dictionary.layout },
            { label: "source", value: dictionary.source },
            { label: "key", value: dictionary.key_columns.join(", ") },
            { label: "lifetime", value: `${dictionary.lifetime_min}…${dictionary.lifetime_max}` },
          ]}
        />
      </section>
      <section className="detail__section" data-testid="card-attributes">
        <Eyebrow as="h4">attributes · {card.attributes.length}</Eyebrow>
        <table className="detail__table">
          <tbody>
            {card.attributes.map((attribute) => (
              <tr key={attribute.name}>
                <td className="detail__col-name">{attribute.name}</td>
                <td className="detail__col-type">{attribute.type}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      {dictionary.create_query !== "" && <Code title="create query" text={dictionary.create_query} mark="card-create-query" />}
    </>
  );
}
