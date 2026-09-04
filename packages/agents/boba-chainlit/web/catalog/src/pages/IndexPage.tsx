import { useEffect, useState, type ReactElement } from "react";
import { Link } from "react-router-dom";

import { ApiError } from "../api/client";
import { useServices } from "../app";
import type { Draft, View } from "../model/catalog";
import { Chip, EmptyState, Eyebrow } from "../ui";

type Lists = { views: View[]; drafts: Draft[] };
type LoadState = { status: "loading" } | { status: "failed"; message: string } | { status: "ready"; lists: Lists };

/** Вход на страницу без адреса вида: доступные виды и открытые черновики. */
export function IndexPage(): ReactElement {
  const { api } = useServices();
  const [state, setState] = useState<LoadState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.views(), api.drafts().catch(() => [] as Draft[])])
      .then(([views, drafts]) => {
        if (!cancelled) {
          setState({ status: "ready", lists: { views, drafts } });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          const message = error instanceof ApiError ? error.detail : String(error);
          setState({ status: "failed", message });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [api]);

  if (state.status === "loading") {
    return <EmptyState fill title="loading" />;
  }

  if (state.status === "failed") {
    return (
      <EmptyState fill title="the catalog is not available">
        {state.message}
      </EmptyState>
    );
  }

  return (
    <div className="index" data-testid="index-page">
      <section className="index__section">
        <Eyebrow as="h4">views</Eyebrow>
        {state.lists.views.length === 0 && <p className="index__empty">no views yet</p>}
        <ul className="index__list">
          {state.lists.views.map((view) => (
            <li key={view.id}>
              <Link to={`/views/${view.id}`} className="index__link">
                {view.name}
              </Link>
            </li>
          ))}
        </ul>
      </section>
      <section className="index__section">
        <Eyebrow as="h4">open drafts</Eyebrow>
        {state.lists.drafts.length === 0 && <p className="index__empty">no open drafts</p>}
        <ul className="index__list">
          {state.lists.drafts.map((draft) => (
            <li key={draft.id}>
              <Link to={`/drafts/${draft.id}`} className="index__link">
                {draft.name}
              </Link>
              <Chip tone="muted">over v{draft.base_version}</Chip>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
