import { ArrowLeft, LogOut, Plug, Workflow } from "lucide-react";
import { type ReactElement, useCallback } from "react";
import { useNavigate } from "react-router-dom";

import { useServices } from "../services";
import { PageUrls } from "../config";
import { Async } from "../components/Async";
import { ThemeToggle } from "../components/ThemeToggle";
import { useLoadable } from "../hooks/useLoadable";
import type { Me } from "../model/account";
import { Button, IconLink, Topbar, TopbarBrand, TopbarCrumbs, TopbarSlot, TopbarSpacer } from "../ui";

/** Личный кабинет: кто вошёл, выход и переход к соединениям. */
export function AccountPage(): ReactElement {
  const { api } = useServices();
  const navigate = useNavigate();
  const [me] = useLoadable(useCallback(() => api.me(), [api]));

  const logout = useCallback(() => {
    void api.logout().then(
      () => {
        void navigate(PageUrls.login(), { replace: true });
      },
      () => {
        void navigate(PageUrls.login(), { replace: true });
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
      <Topbar>
        <TopbarSlot />
        <TopbarBrand>
          <Workflow size={18} />
          <b>Boba</b> Workflow <span>Studio</span>
        </TopbarBrand>
        <TopbarCrumbs root="Account" />
        <TopbarSpacer />
        <Button onClick={logout} aria-label="Sign out">
          <LogOut size={14} />
          Sign out
        </Button>
        <ThemeToggle />
        {/* стрелка назад живёт на месте шестерёнки: UI не прыгает при переходе */}
        <IconLink to={PageUrls.workflow()} aria-label="Back to studio" title="Back to studio">
          <ArrowLeft size={16} />
        </IconLink>
      </Topbar>
      <div className="account__body">
        <Async state={me} render={renderHeader} />
        <div>
          <Button
            icon={Plug}
            onClick={() => {
              void navigate(PageUrls.connections());
            }}
            data-testid="account-connections"
          >
            Connections
          </Button>
        </div>
      </div>
    </div>
  );
}
