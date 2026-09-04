import { KeyRound } from "lucide-react";
import type { ReactElement, ReactNode } from "react";

import type { ObjectCard } from "../../model/catalog";
import { Cell, Chip, Code, DataTable, Facts, Panel, PanelHead, Section, TableRow, type Fact } from "../../ui";

type Props = {
  card: ObjectCard;
  /** Кнопки справа в шапке: правка и удаление на черновике ручного источника. */
  actions?: ReactNode;
  /** Панель внутри секции другой панели: без своих отступов и ограничения ширины. */
  flat?: boolean;
};

/** Родная карточка объекта источника: у Postgres и ClickHouse свой набор
 * фактов и таблиц, ничего не приводится к общему виду. */
export function ObjectCardPanel({ card, actions, flat = false }: Props): ReactElement {
  return (
    <div data-testid="object-card" data-card={card.card} data-path={card.ref.path.join("/")}>
      <Panel page={!flat} flat={flat}>
        {card.card === "pg_relation" && <PgRelationView card={card} actions={actions} />}
        {card.card === "pg_routine" && <PgRoutineView card={card} actions={actions} />}
        {card.card === "pg_sequence" && <PgSequenceView card={card} actions={actions} />}
        {card.card === "pg_type" && <PgTypeView card={card} actions={actions} />}
        {card.card === "ch_table" && <ChTableView card={card} actions={actions} />}
        {card.card === "ch_dictionary" && <ChDictionaryView card={card} actions={actions} />}
      </Panel>
    </div>
  );
}

type Head = {
  eyebrow: string;
  name: string;
  kind: string;
  comment: string | null;
  actions: ReactNode;
};

function CardHead({ eyebrow, name, kind, comment, actions }: Head): ReactElement {
  return (
    <PanelHead
      eyebrow={eyebrow}
      name={name}
      mono
      actions={
        <>
          <Chip tone="muted">{kind}</Chip>
          {actions}
        </>
      }
      description={comment !== null && comment !== "" ? <span data-testid="card-comment">{comment}</span> : undefined}
    />
  );
}

type Raw = {
  label: string;
  value: string | number | boolean | null | undefined;
};

/** Факты карточки: пустые значения не показываются. */
function CardFacts({ facts }: { facts: Raw[] }): ReactElement {
  const shown: Fact[] = [];
  for (const fact of facts) {
    if (fact.value === null || fact.value === undefined || fact.value === "") {
      continue;
    }

    shown.push({
      key: fact.label,
      label: fact.label,
      value: <span className="mono">{String(fact.value)}</span>,
    });
  }

  return (
    <Section>
      <Facts facts={shown} mark="card-facts" />
    </Section>
  );
}

function CodeSection({ title, text, mark }: { title: string; text: string; mark: string }): ReactElement {
  return (
    <Section title={title} mark={mark}>
      <Code>{text}</Code>
    </Section>
  );
}

function Lines({ title, mark, children }: { title: string; mark: string; children: ReactNode }): ReactElement {
  return (
    <Section title={title} mark={mark}>
      <DataTable>{children}</DataTable>
    </Section>
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

type Viewer<T extends ObjectCard["card"]> = {
  card: Extract<ObjectCard, { card: T }>;
  actions: ReactNode;
};

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
      <CardFacts
        facts={[
          { label: "owner", value: relation.owner },
          { label: "rows", value: relation.row_estimate },
          { label: "size", value: bytes(relation.total_bytes) },
          {
            label: "persistence",
            value: relation.persistence === "permanent" ? "" : relation.persistence,
          },
          { label: "tablespace", value: relation.tablespace },
          { label: "partition key", value: relation.partition_key },
          { label: "partition of", value: relation.partition_of },
          { label: "bound", value: relation.partition_bound },
          { label: "check option", value: relation.check_option },
          { label: "populated", value: relation.populated },
          { label: "foreign server", value: relation.foreign_server },
        ]}
      />
      <Section title={`columns · ${card.columns.length}`} scroll mark="card-columns">
        <DataTable>
          {card.columns.map((column) => (
            <TableRow key={column.name} data-column={column.name}>
              <Cell mod="icon">{isKey(card, column.name) && <KeyRound size={11} />}</Cell>
              <Cell data-col="name">{column.name}</Cell>
              <Cell mod="dim" data-col="type">
                {column.type}
              </Cell>
              <Cell mod="dim" data-col="null">
                {column.nullable ? "null" : "not null"}
              </Cell>
              <Cell mod="wrap" data-col="extra">
                {column.default !== null && <span>default {column.default} </span>}
                {column.identity !== null && <span>identity {column.identity} </span>}
                {column.generated !== null && <span>generated {column.generated}</span>}
              </Cell>
              <Cell mod="wrap" data-col="comment">
                {column.comment ?? ""}
              </Cell>
            </TableRow>
          ))}
        </DataTable>
      </Section>
      {card.constraints.length > 0 && (
        <Lines title={`constraints · ${card.constraints.length}`} mark="card-constraints">
          {card.constraints.map((constraint) => (
            <TableRow key={constraint.name}>
              <Cell mod="icon">
                <Chip tone="muted">{constraint.kind}</Chip>
              </Cell>
              <Cell data-col="name">{constraint.name}</Cell>
              <Cell mod="wrap">{constraint.definition}</Cell>
            </TableRow>
          ))}
        </Lines>
      )}
      {card.indexes.length > 0 && (
        <Lines title={`indexes · ${card.indexes.length}`} mark="card-indexes">
          {card.indexes.map((index) => (
            <TableRow key={index.name}>
              <Cell data-col="name">{index.name}</Cell>
              <Cell mod="wrap">{index.definition}</Cell>
            </TableRow>
          ))}
        </Lines>
      )}
      {card.partitions.length > 0 && (
        <Lines title={`partitions · ${card.partitions.length}`} mark="card-partitions">
          {card.partitions.map((partition) => (
            <TableRow key={partition.name}>
              <Cell data-col="name">{partition.name}</Cell>
              <Cell mod="wrap">{partition.partition_bound ?? ""}</Cell>
            </TableRow>
          ))}
        </Lines>
      )}
      {relation.definition !== null && (
        <CodeSection title="definition" text={relation.definition} mark="card-definition" />
      )}
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
      <CardFacts
        facts={[
          { label: "language", value: routine.language },
          {
            label: "returns",
            value: routine.returns_set ? `setof ${routine.returns ?? ""}` : routine.returns,
          },
          { label: "volatility", value: routine.volatility },
          { label: "strict", value: routine.strict },
          {
            label: "security definer",
            value: routine.security_definer ? true : "",
          },
          { label: "parallel", value: routine.parallel },
          { label: "owner", value: routine.owner },
        ]}
      />
      <Section title={`arguments · ${card.arguments.length}`} scroll mark="card-arguments">
        <DataTable>
          {card.arguments.map((argument) => (
            <TableRow key={argument.position}>
              <Cell data-col="name">{argument.name ?? `$${argument.position + 1}`}</Cell>
              <Cell mod="dim" data-col="type">
                {argument.type}
              </Cell>
              <Cell mod="dim" data-col="null">
                {argument.mode}
              </Cell>
              <Cell mod="wrap" data-col="extra">
                {argument.default !== null && <span>default {argument.default}</span>}
              </Cell>
            </TableRow>
          ))}
        </DataTable>
      </Section>
      <CodeSection title="body" text={routine.body} mark="card-body" />
      {routine.definition !== "" && <CodeSection title="definition" text={routine.definition} mark="card-definition" />}
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
      <CardFacts
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
      <CardFacts
        facts={[
          { label: "owner", value: type.owner },
          { label: "labels", value: type.labels?.join(", ") },
          { label: "base type", value: type.base_type },
          { label: "constraint", value: type.constraint },
          {
            label: "attributes",
            value: type.attributes?.map((a) => `${a.name} ${a.type}`).join(", "),
          },
        ]}
      />
    </>
  );
}

function ChTableView({ card, actions }: Viewer<"ch_table">): ReactElement {
  const table = card.table;
  return (
    <>
      <CardHead
        eyebrow={table.database}
        name={table.name}
        kind={table.kind}
        comment={table.comment}
        actions={actions}
      />
      <CardFacts
        facts={[
          {
            label: "engine",
            value: table.engine_full === "" ? table.engine : table.engine_full,
          },
          { label: "rows", value: table.total_rows },
          {
            label: "size",
            value: table.total_bytes === null ? "" : bytes(table.total_bytes),
          },
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
      <Section title={`columns · ${card.columns.length}`} scroll mark="card-columns">
        <DataTable>
          {card.columns.map((column) => (
            <TableRow key={column.name} data-column={column.name}>
              <Cell mod="icon">{column.in_primary_key && <KeyRound size={11} />}</Cell>
              <Cell data-col="name">{column.name}</Cell>
              <Cell mod="dim" data-col="type">
                {column.type}
              </Cell>
              <Cell mod="wrap" data-col="extra">
                {column.default_kind !== null && (
                  <span>
                    {column.default_kind} {column.default_expression ?? ""}{" "}
                  </span>
                )}
                {column.codec !== null && <span>codec {column.codec} </span>}
                {column.ttl !== null && <span>ttl {column.ttl}</span>}
              </Cell>
              <Cell mod="wrap" data-col="comment">
                {column.comment ?? ""}
              </Cell>
            </TableRow>
          ))}
        </DataTable>
      </Section>
      {table.definition !== null && <CodeSection title="definition" text={table.definition} mark="card-definition" />}
      {table.create_query !== "" && (
        <CodeSection title="create query" text={table.create_query} mark="card-create-query" />
      )}
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
      <CardFacts
        facts={[
          { label: "status", value: dictionary.status },
          { label: "layout", value: dictionary.layout },
          { label: "source", value: dictionary.source },
          { label: "key", value: dictionary.key_columns.join(", ") },
          {
            label: "lifetime",
            value: `${dictionary.lifetime_min}…${dictionary.lifetime_max}`,
          },
        ]}
      />
      <Section title={`attributes · ${card.attributes.length}`} scroll mark="card-attributes">
        <DataTable>
          {card.attributes.map((attribute) => (
            <TableRow key={attribute.name}>
              <Cell data-col="name">{attribute.name}</Cell>
              <Cell mod="dim" data-col="type">
                {attribute.type}
              </Cell>
            </TableRow>
          ))}
        </DataTable>
      </Section>
      {dictionary.create_query !== "" && (
        <CodeSection title="create query" text={dictionary.create_query} mark="card-create-query" />
      )}
    </>
  );
}
