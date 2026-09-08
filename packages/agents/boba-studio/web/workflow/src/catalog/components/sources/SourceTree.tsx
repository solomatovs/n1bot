import { ChevronDown, ChevronRight } from "lucide-react";
import { useCallback, useState, type CSSProperties, type ReactElement } from "react";

import { useLoadable } from "../../../hooks/useLoadable";
import { sameRef, type ObjectRef, type TreeNode } from "../../model/catalog";
import { OBJECT_DRAG_TYPE, ObjectRefParam } from "../../model/refParam";
import { IconButton, Note } from "../../../ui";
import "./sources.css";

/** Загрузчик детей узла: у версии источника и у черновика свой. */
export type TreeLoader = (path: string[]) => Promise<TreeNode[]>;

type Props = {
  load: TreeLoader;
  /** Смена ключа перечитывает дерево с корня. */
  reloadKey: string;
  selected: ObjectRef | undefined;
  onSelect: (node: TreeNode) => void;
  /** Объекты можно тащить на холст процесса: в перетаскивании едет адрес. */
  draggable?: boolean;
};

/** Дерево источника любой глубины: уровни не зашиты, дети догружаются по
 * раскрытию (сервер читает только их), узлы с адресом выбираются. */
export function SourceTree({ load, reloadKey, selected, onSelect, draggable = false }: Props): ReactElement {
  return (
    <div className="tree" data-testid="source-tree" data-reload={reloadKey}>
      <Branch
        load={load}
        reloadKey={reloadKey}
        path={[]}
        depth={0}
        selected={selected}
        onSelect={onSelect}
        draggable={draggable}
      />
    </div>
  );
}

type BranchProps = Omit<Props, "draggable"> & {
  path: string[];
  depth: number;
  draggable: boolean;
};

/** Ступени пути одной строкой: ключ зависимости эффекта и обратно в ступени. */
const PathKey = {
  SEPARATOR: "\0",

  render(path: string[]): string {
    return path.join(PathKey.SEPARATOR);
  },

  parse(key: string): string[] {
    if (key === "") {
      return [];
    }

    return key.split(PathKey.SEPARATOR);
  },
};

function Branch({ load, reloadKey, path, depth, selected, onSelect, draggable }: BranchProps): ReactElement {
  const pathKey = PathKey.render(path);
  const loadNodes = useCallback(() => load(PathKey.parse(pathKey)), [load, pathKey]);
  const [state] = useLoadable(loadNodes, { key: reloadKey });

  if (state.kind === "loading") {
    return (
      <Note pad micro mark="tree-note">
        loading…
      </Note>
    );
  }

  if (state.kind === "error") {
    return (
      <Note pad micro tone="error" mark="tree-note">
        {state.message}
      </Note>
    );
  }

  if (state.value.length === 0) {
    return (
      <Note pad micro mark="tree-note">
        empty
      </Note>
    );
  }

  return (
    <ul className="tree__list" data-depth={depth}>
      {state.value.map((node) => (
        <Leaf
          key={node.path.join("/")}
          node={node}
          load={load}
          reloadKey={reloadKey}
          depth={depth}
          selected={selected}
          onSelect={onSelect}
          draggable={draggable}
        />
      ))}
    </ul>
  );
}

type LeafProps = Omit<BranchProps, "path"> & { node: TreeNode };

function Leaf({ node, load, reloadKey, depth, selected, onSelect, draggable }: LeafProps): ReactElement {
  const [open, setOpen] = useState(false);
  const expandable = node.expandable;
  let isSelected = false;
  if (selected !== undefined && node.ref !== null) {
    isSelected = sameRef(selected, node.ref);
  }

  const toggle = useCallback(() => {
    setOpen((current) => !current);
  }, []);

  return (
    <li
      className="tree__item"
      data-testid="tree-node"
      data-path={node.path.join("/")}
      data-kind={node.kind}
      data-open={open}
      data-selected={isSelected}
    >
      <div className="tree__row" style={{ "--depth": depth } as CSSProperties}>
        {expandable ? (
          <IconButton size="sm" ghost aria-label={`${open ? "collapse" : "expand"} ${node.label}`} onClick={toggle}>
            {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </IconButton>
        ) : (
          <span className="tree__spacer" />
        )}
        {node.ref === null ? (
          <button type="button" className="tree__label tree__label--container" onClick={toggle}>
            <span className="tree__name">{node.label}</span>
          </button>
        ) : (
          <button
            type="button"
            className="tree__label"
            aria-pressed={isSelected}
            draggable={draggable}
            onDragStart={(event) => {
              if (node.ref === null) {
                return;
              }

              event.dataTransfer.setData(OBJECT_DRAG_TYPE, ObjectRefParam.render(node.ref));
              event.dataTransfer.effectAllowed = "copy";
            }}
            onClick={() => {
              onSelect(node);
            }}
          >
            <span className="tree__name">{node.label}</span>
            {node.detail !== "" && <span className="tree__detail">{node.detail}</span>}
          </button>
        )}
      </div>
      {open && expandable && (
        <Branch
          load={load}
          reloadKey={reloadKey}
          path={node.path}
          depth={depth + 1}
          selected={selected}
          onSelect={onSelect}
          draggable={draggable}
        />
      )}
    </li>
  );
}
