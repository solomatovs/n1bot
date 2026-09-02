import { createContext, type ReactElement, useContext, useEffect, useMemo, useRef } from "react";
import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";

import { WorkflowApi } from "./api/client";
import { RunSocket } from "./api/socket";
import { Shell } from "./components/shell/Shell";
import { PageUrls, pageConfig } from "./config";
import { AccountPage } from "./pages/AccountPage";
import { BuildPage } from "./pages/BuildPage";
import { LoginPage } from "./pages/LoginPage";
import { ObservePage } from "./pages/ObservePage";

/** Общие для страниц службы: адреса и API-клиент.
 *
 * Профиль studio не выбирает: запросы идут без него, сервер берёт профиль
 * по умолчанию (general). Профили — механика chainlit; их роль здесь со
 * временем займут сами workflow. */
export type Services = {
  urls: PageUrls;
  api: WorkflowApi;
  socket: RunSocket;
};

const ServicesContext = createContext<Services | null>(null);

export function useServices(): Services {
  const services = useContext(ServicesContext);
  if (services === null) {
    throw new Error("services are provided by App only");
  }

  return services;
}

function LegacyRun(): ReactElement {
  const { runId } = useParams();
  return <Navigate to={`/runs/${runId ?? ""}`} replace />;
}

function LegacyWorkflow(): ReactElement {
  const { workflowId } = useParams();
  return <Navigate to={`/workflow/${workflowId ?? ""}`} replace />;
}

function LegacyBuild(): ReactElement {
  const { workflowId } = useParams();
  const { search } = useLocation();
  if (workflowId === "new") {
    return <Navigate to={`/workflow/new${search}`} replace />;
  }

  return <Navigate to={`/workflow/${workflowId ?? ""}`} replace />;
}

/** 401 от api в любом месте уводит на вход, запоминая, откуда ушли. */
function SignedInOnly(): ReactElement {
  const { api } = useServices();
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    api.onUnauthorized(() => {
      void navigate("/login", { replace: true, state: { next: `${location.pathname}${location.search}` } });
    });

    return () => {
      api.onUnauthorized(null);
    };
  }, [api, navigate, location.pathname, location.search]);

  return <Outlet />;
}

export function App(): ReactElement {
  const urls = useMemo(() => new PageUrls(pageConfig()), []);
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
        void new WorkflowApi(urls)
          .refreshSession()
          .catch(() => false)
          .then(() => {
            refreshing.current = false;
          });
      }),
    [liveSocket, urls],
  );
  const services = useMemo<Services>(
    () => ({ urls, api: new WorkflowApi(urls), socket: liveSocket }),
    [urls, liveSocket],
  );

  return (
    <ServicesContext.Provider value={services}>
      <BrowserRouter basename={services.urls.routerBase}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route element={<SignedInOnly />}>
            <Route path="/account" element={<AccountPage />} />
            <Route element={<Shell />}>
              <Route path="/workflow" element={<BuildPage />} />
              <Route path="/workflow/new" element={<BuildPage />} />
              <Route path="/workflow/:workflowId" element={<BuildPage />} />
              <Route path="/runs/:runId" element={<ObservePage />} />
            </Route>
          </Route>
          <Route path="/run/:runId" element={<LegacyRun />} />
          <Route path="/observe/:runId" element={<LegacyRun />} />
          <Route path="/observe" element={<Navigate to="/workflow" replace />} />
          <Route path="/build/:workflowId" element={<LegacyBuild />} />
          <Route path="/build" element={<Navigate to="/workflow" replace />} />
          <Route path="/w/:workflowId" element={<LegacyWorkflow />} />
          <Route path="/new" element={<Navigate to="/workflow/new" replace />} />
          <Route path="*" element={<Navigate to="/workflow" replace />} />
        </Routes>
      </BrowserRouter>
    </ServicesContext.Provider>
  );
}
