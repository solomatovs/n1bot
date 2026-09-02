import { ArrowLeft, LogOut, Workflow } from "lucide-react";
import { type ReactElement, useCallback, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useServices } from "../app";
import { Async } from "../components/Async";
import { ConnectionsTab } from "../components/account/ConnectionsTab";
import { Segmented } from "../components/Segmented";
import { ThemeToggle } from "../components/ThemeToggle";
import { useLoadable } from "../hooks/useLoadable";
import type { Me } from "../model/account";

type Tab = "connections";

const TABS: { value: Tab; label: string }[] = [{ value: "connections", label: "Connections" }];

/** Содержимое вкладок по ключу: новая вкладка — новая запись здесь и в TABS. */
const TAB_VIEWS: Record<Tab, () => ReactElement> = {
  connections: () => <ConnectionsTab />,
};

/** Личный кабинет: кто вошёл, выход и вкладки приватных настроек. */
export function AccountPage(): ReactElement {
  const { api } = useServices();
  const navigate = useNavigate();
  const [me] = useLoadable(useCallback(() => api.me(), [api]));
  const [tab, setTab] = useState<Tab>("connections");

  const logout = useCallback(() => {
    void api.logout().then(
      () => {
        void navigate("/login", { replace: true });
      },
      () => {
        void navigate("/login", { replace: true });
      },
    );
  }, [api, navigate]);

  const renderHeader = (value: Me): ReactElement => (
    <div className="account__who">
      <span className="account__login">{value.login}</span>
      <span className="account__meta">
        roles: {value.roles.length === 0 ? "—" : value.roles.join(", ")} · sign-in:{" "}
        {value.sign_in.provider === "" ? "—" : value.sign_in.provider}
        {value.sign_in.principal !== "" && ` (${value.sign_in.principal})`}
        {value.sign_in.ticket && " · delegated ticket"}
      </span>
    </div>
  );

  return (
    <div className="account">
      <header className="topbar">
        <Link to="/workflow" className="icon-btn" aria-label="Back to studio" title="Back to studio">
          <ArrowLeft size={16} />
        </Link>
        <div className="topbar__brand">
          <Workflow size={18} />
          <b>Boba</b> Workflow <span>Studio</span>
        </div>
        <nav className="crumbs" aria-label="breadcrumbs">
          <span>Account</span>
        </nav>
        <span className="topbar__spacer" />
        <button type="button" className="btn" onClick={logout} aria-label="Sign out">
          <LogOut size={14} />
          Sign out
        </button>
        <ThemeToggle />
      </header>
      <div className="account__body">
        <Async state={me} render={renderHeader} />
        <Segmented options={TABS} value={tab} onChange={setTab} label="account sections" />
        {TAB_VIEWS[tab]()}
      </div>
    </div>
  );
}
