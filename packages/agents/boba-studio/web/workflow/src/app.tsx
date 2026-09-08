import { type ReactElement, useCallback, useEffect, useMemo, useRef } from "react";
import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";

import { WorkflowApi } from "./api/client";
import { RunSocket } from "./api/socket";
import { HttpTransport } from "./api/transport";
import { catalogRoutes } from "./catalog/services";
import { ServicesContext, useServices, type Services } from "./services";
import { Shell } from "./components/shell/Shell";
import { PageUrls, pageConfig } from "./config";
import { ToastProvider } from "./ui";
import { AccountPage } from "./pages/AccountPage";
import { BuildPage } from "./pages/BuildPage";
import { LoginPage } from "./pages/LoginPage";
import { ObservePage } from "./pages/ObservePage";

function LegacyRun(): ReactElement {
  const { runId } = useParams();
  return <Navigate to={PageUrls.run(runId ?? "")} replace />;
}

function LegacyWorkflow(): ReactElement {
  const { workflowId } = useParams();
  return <Navigate to={PageUrls.workflow(workflowId ?? "")} replace />;
}

function LegacyBuild(): ReactElement {
  const { workflowId } = useParams();
  if (workflowId === "new") {
    return <Navigate to={PageUrls.workflow()} replace />;
  }

  return <Navigate to={PageUrls.workflow(workflowId ?? "")} replace />;
}

/** Куда уводить на 401: вход с памятью, откуда ушли. Обработчик один на
 * всё время жизни страницы: адрес читается в момент отказа, иначе смена
 * адреса пересобирала бы службы, которые на него подписаны. */
function useSignIn(): () => void {
  const navigate = useNavigate();
  const location = useLocation();
  const current = useRef({ navigate, location });
  current.current = { navigate, location };

  return useCallback(() => {
    const { navigate: go, location: at } = current.current;
    void go(PageUrls.login(), { replace: true, state: { next: `${at.pathname}${at.search}` } });
  }, []);
}

/** 401 от api в любом месте уводит на вход, запоминая, откуда ушли: один
 * обработчик на транспорте, общий для всех клиентов. */
function SignedInOnly(): ReactElement {
  const { transport } = useServices();
  const signIn = useSignIn();

  useEffect(() => {
    transport.onUnauthorized(signIn);

    return () => {
      transport.onUnauthorized(null);
    };
  }, [transport, signIn]);

  return <Outlet />;
}

export function App(): ReactElement {
  const urls = useMemo(() => new PageUrls(pageConfig()), []);
  const transport = useMemo(() => new HttpTransport(urls), [urls]);
  const socket = useRef<RunSocket | null>(null);
  socket.current ??= new RunSocket(urls);
  const liveSocket = socket.current;

  // вход на исходе: сервер просит молча обновить сессию, один обмен за раз
  const refreshing = useRef(false);
  useEffect(
    () =>
      liveSocket.onUser((event) => {
        if (event.kind !== "signin_refresh_requested" || refreshing.current) {
          return;
        }

        refreshing.current = true;
        void new WorkflowApi(transport)
          .refreshSession()
          .catch(() => false)
          .then(() => {
            refreshing.current = false;
          });
      }),
    [liveSocket, transport],
  );
  const services = useMemo<Services>(
    () => ({ urls, transport, api: new WorkflowApi(transport), socket: liveSocket }),
    [urls, transport, liveSocket],
  );

  return (
    <ServicesContext.Provider value={services}>
      <BrowserRouter basename={services.urls.routerBase}>
        <ToastProvider>
        <Routes>
          <Route path={PageUrls.login()} element={<LoginPage />} />
          <Route element={<SignedInOnly />}>
            <Route path={PageUrls.account()} element={<AccountPage />} />
            {catalogRoutes()}
            <Route element={<Shell />}>
              <Route path={PageUrls.workflow()} element={<BuildPage />} />
              <Route path={PageUrls.workflow(":workflowId")} element={<BuildPage />} />
              <Route path={PageUrls.run(":runId")} element={<ObservePage />} />
            </Route>
          </Route>
          <Route path="/run/:runId" element={<LegacyRun />} />
          <Route path="/observe/:runId" element={<LegacyRun />} />
          <Route path="/observe" element={<Navigate to={PageUrls.workflow()} replace />} />
          <Route path="/build/:workflowId" element={<LegacyBuild />} />
          <Route path="/build" element={<Navigate to={PageUrls.workflow()} replace />} />
          <Route path="/w/:workflowId" element={<LegacyWorkflow />} />
          <Route path="/new" element={<Navigate to={PageUrls.workflow()} replace />} />
          <Route path="*" element={<Navigate to={PageUrls.workflow()} replace />} />
        </Routes>
        </ToastProvider>
      </BrowserRouter>
    </ServicesContext.Provider>
  );
}
