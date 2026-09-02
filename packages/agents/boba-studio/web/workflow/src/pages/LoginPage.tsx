import { LogIn, Workflow } from "lucide-react";
import { type FormEvent, type ReactElement, useCallback, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { z } from "zod";

import { ApiError } from "../api/client";
import { useServices } from "../app";
import { Alert } from "../ui/Alert";
import { Async, errorText } from "../components/Async";
import { ThemeToggle } from "../components/ThemeToggle";
import { useLoadable } from "../hooks/useLoadable";
import type { SignInProviders } from "../model/account";
import { Button, Field, Input, LinkButton } from "../ui";

const NextStateSchema = z.object({ next: z.string() });

/** Исход SSO из ?error= — тексты для человека. */
const SSO_ERRORS: Record<string, string> = {
  sso_ticket: "SSO: the browser did not present a Kerberos ticket",
  sso_denied: "SSO: access denied",
  sso_failed: "SSO: sign-in failed",
};

/** Куда вернуться после входа: откуда увели, иначе — история запусков. */
function nextOf(state: unknown): string {
  const parsed = NextStateSchema.safeParse(state);
  if (!parsed.success) {
    return "/workflow";
  }

  if (parsed.data.next === "" || parsed.data.next.startsWith("/login")) {
    return "/workflow";
  }

  return parsed.data.next;
}

/** Вход: логин и пароль через api; SSO — кнопкой на chainlit, если настроен. */
export function LoginPage(): ReactElement {
  const { api, urls } = useServices();
  const navigate = useNavigate();
  const location = useLocation();
  const [search] = useSearchParams();
  const [providers] = useLoadable(useCallback(() => api.providers(), [api]));
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(() => SSO_ERRORS[search.get("error") ?? ""] ?? "");
  const next = nextOf(location.state);

  const submit = useCallback(
    (event: FormEvent) => {
      event.preventDefault();
      setBusy(true);
      setNotice("");
      void api.login(username, password).then(
        () => {
          void navigate(next, { replace: true });
        },
        (failure: unknown) => {
          setBusy(false);
          if (failure instanceof ApiError) {
            setNotice(failure.detail);
            return;
          }

          setNotice(errorText(failure));
        },
      );
    },
    [api, username, password, navigate, next],
  );

  const renderForm = (available: SignInProviders): ReactElement => (
    <>
      {available.password && (
        <form className="form login__form" onSubmit={submit} aria-label="sign in">
          <Field label="login">
            <Input
              name="username"
              autoComplete="username"
              value={username}
              onChange={(event) => {
                setUsername(event.target.value);
              }}
              required
            />
          </Field>
          <Field label="password">
            <Input
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => {
                setPassword(event.target.value);
              }}
              required
            />
          </Field>
          {notice !== "" && (
            <Alert tone="error" mark="login">
              {notice}
            </Alert>
          )}
          <Button type="submit" tone="primary" className="login__submit" disabled={busy}>
            <LogIn size={14} />
            Sign in
          </Button>
        </form>
      )}
      {available.sso_url !== "" && (
        <LinkButton className="login__sso" href={`${available.sso_url}?next=${encodeURIComponent(urls.routerBase + next)}`}>
          Sign in with SSO
        </LinkButton>
      )}
      {!available.password && available.sso_url === "" && (
        <Alert tone="error">No sign-in method is configured</Alert>
      )}
    </>
  );

  return (
    <div className="login">
      <div className="login__card">
        <div className="login__brand">
          <Workflow size={22} />
          <b>Boba</b> Workflow <span>Studio</span>
          <span className="topbar__spacer" />
          <ThemeToggle />
        </div>
        <Async state={providers} render={renderForm} />
      </div>
    </div>
  );
}
