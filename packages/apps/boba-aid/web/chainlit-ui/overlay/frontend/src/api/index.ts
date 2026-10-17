import getRouterBasename from '@/lib/router';
import i18n from 'i18next';
import { toast } from 'sonner';

import { ChainlitAPI, ClientError } from '@chainlit/react-client';

const devServer =
  (import.meta.env.VITE_API_URL || 'http://localhost:8000') +
  getRouterBasename();
const url = import.meta.env.DEV
  ? devServer
  : window.origin + getRouterBasename();
const serverUrl = new URL(url);

const httpEndpoint = serverUrl.toString();

const on401 = () => {
  if (window.location.pathname !== getRouterBasename() + '/login') {
    // The credentials aren't correct, remove the token and redirect to login
    window.location.href = getRouterBasename() + '/login';
  }
};

// boba: отказ входа и сессии приходит кодом (detail) — его текст берётся из
// переводов auth.login.errors, как у баннера страницы входа; 401 без кода —
// сессия кончилась. Прочие ошибки показываются как пришли.
const LOGIN_ERRORS = 'auth.login.errors';
const SESSION_REQUIRED = `${LOGIN_ERRORS}.sessionRequired`;

const errorText = (error: ClientError): string => {
  if (error.detail) {
    const key = `${LOGIN_ERRORS}.${error.detail.toLowerCase()}`;
    if (i18n.exists(key)) return i18n.t(key);
  }
  if (error.status === 401 && i18n.exists(SESSION_REQUIRED)) {
    return i18n.t(SESSION_REQUIRED);
  }
  return error.toString();
};

const onError = (error: ClientError) => {
  toast.error(errorText(error));
};

class ExtendedChainlitAPI extends ChainlitAPI {
  async shareThread(
    threadId: string,
    isShared: boolean
  ): Promise<{ success: boolean }> {
    const res = await this.put(`/project/thread/share`, {
      threadId,
      isShared
    });
    return res.json();
  }

  connectStreamableHttpMCP(
    sessionId: string,
    name: string,
    url: string,
    headers?: Record<string, string>
  ) {
    // Assumes the backend expects { clientType, name, url }
    return fetch(
      new URL(
        'mcp',
        this.httpEndpoint.endsWith('/')
          ? this.httpEndpoint
          : `${this.httpEndpoint}/`
      ),
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(sessionId ? { 'x-session-id': sessionId } : {})
        },
        body: JSON.stringify({
          clientType: 'streamable-http',
          name,
          url,
          sessionId,
          ...(headers ? { headers } : {})
        })
      }
    ).then(async (res) => {
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to connect MCP');
      }
      return { success: true, mcp: data.mcp };
    });
  }
}

export const apiClient = new ExtendedChainlitAPI(
  httpEndpoint,
  'webapp',
  {}, // Optional - additionalQueryParams property.
  on401,
  onError
);
