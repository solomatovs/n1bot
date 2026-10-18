import { MessageContext } from 'contexts/MessageContext';
import {
  type ComponentType,
  memo,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState
} from 'react';
import { importCode } from 'react-runner';
import { useRecoilValue } from 'recoil';
import { v4 as uuidv4 } from 'uuid';

import {
  ChainlitContext,
  IAction,
  ICustomElement,
  IElement,
  sessionIdState,
  useAuth,
  useChatInteract
} from '@chainlit/react-client';

import Alert from '@/components/Alert';
import { ErrorBoundary } from '@/components/ErrorBoundary';

import Imports from './Imports';
import * as Renderer from './Renderer';

type Evaluated = {
  component?: ComponentType;
  inPlace?: boolean;
  error?: string;
};

// boba: исходник элемента исполняется один раз, а не на каждый рендер. Runner
// собирал компонент заново при любой смене scope, а scope создавался на каждый
// рендер: смена loading или askUser в MessageContext перемонтировала элемент, и
// приложение MCP Apps в нём грузилось заново. props и колбэки элемент читает
// через live — значения последнего рендера. Новые props, как и у upstream,
// монтируют элемент заново; элемент, объявивший export const updatesInPlace =
// true, получает их тем же компонентом.
const CustomElement = memo(function ({ element }: { element: ICustomElement }) {
  const apiClient = useContext(ChainlitContext);
  const sessionId = useRecoilValue(sessionIdState);
  const { sendMessage } = useChatInteract();
  const { user } = useAuth();
  const { askUser } = useContext(MessageContext);

  const [sourceCode, setSourceCode] = useState<string>();
  const [error, setError] = useState<string>();

  useEffect(() => {
    apiClient
      .get(`/public/elements/${element.name}.jsx`)
      .then(async (res) => setSourceCode(await res.text()))
      .catch((err) => setError(String(err)));
  }, [element.name, apiClient]);

  const updateElement = useCallback(
    (nextProps: Record<string, unknown>) => {
      if (!sessionId) return;
      const nextElement: IElement = { ...element, props: nextProps };
      return apiClient.updateElement(nextElement, sessionId);
    },
    [element, sessionId, apiClient]
  );

  const deleteElement = useCallback(() => {
    if (!sessionId) return;
    return apiClient.deleteElement(element, sessionId);
  }, [element, sessionId, apiClient]);

  const callAction = useCallback(
    (action: IAction) => {
      if (!sessionId) return;
      return apiClient.callAction(action, sessionId);
    },
    [sessionId, apiClient]
  );

  const sendUserMessage = useCallback(
    (message: string, command?: string) => {
      return sendMessage({
        threadId: '',
        id: uuidv4(),
        name: user?.identifier || 'User',
        type: 'user_message',
        output: message,
        createdAt: new Date().toISOString(),
        metadata: { location: window.location.href },
        command
      });
    },
    [sendMessage, user]
  );

  const submitElement = useCallback(
    (props: Record<string, unknown>) => {
      if (
        askUser?.spec.type === 'element' &&
        askUser.spec.step_id === element.forId
      ) {
        askUser.callback({ ...props, submitted: true });
      }
    },
    [askUser, element.forId]
  );

  const cancelElement = useCallback(() => {
    if (
      askUser?.spec.type === 'element' &&
      askUser.spec.step_id === element.forId
    ) {
      askUser.callback({ submitted: false });
    }
  }, [askUser, element.forId]);

  const props = useMemo(() => {
    return JSON.parse(JSON.stringify(element.props));
  }, [element.props]);

  const live = useRef({
    props,
    updateElement,
    deleteElement,
    callAction,
    sendUserMessage,
    submitElement,
    cancelElement
  });
  live.current = {
    props,
    updateElement,
    deleteElement,
    callAction,
    sendUserMessage,
    submitElement,
    cancelElement
  };

  const evaluated = useMemo<Evaluated | null>(() => {
    if (!sourceCode) return null;

    const now = () => live.current;
    const liveProps = new Proxy(
      {},
      {
        get: (_target, key) => Reflect.get(now().props, key),
        has: (_target, key) => Reflect.has(now().props, key),
        ownKeys: () => Reflect.ownKeys(now().props),
        getOwnPropertyDescriptor: (_target, key) => {
          const found = Reflect.getOwnPropertyDescriptor(now().props, key);
          if (found) found.configurable = true;
          return found;
        }
      }
    );
    const scope = {
      import: { ...Imports, '@/components/renderer': Renderer },
      props: liveProps,
      apiClient,
      updateElement: (next: Record<string, unknown>) =>
        now().updateElement(next),
      deleteElement: () => now().deleteElement(),
      callAction: (action: IAction) => now().callAction(action),
      sendUserMessage: (message: string, command?: string) =>
        now().sendUserMessage(message, command),
      submitElement: (next: Record<string, unknown>) =>
        now().submitElement(next),
      cancelElement: () => now().cancelElement()
    };

    try {
      const exported = importCode(sourceCode, scope);
      if (typeof exported.default !== 'function') {
        return { error: `${element.name}.jsx exports no default component` };
      }
      return {
        component: exported.default,
        inPlace: exported.updatesInPlace === true
      };
    } catch (failed) {
      return { error: String(failed) };
    }
  }, [sourceCode, apiClient, element.name]);

  const generation = useMemo(() => uuidv4(), [element.props]);

  if (error) return <Alert variant="error">{error}</Alert>;
  if (!evaluated) return null;
  if (evaluated.error) return <Alert variant="error">{evaluated.error}</Alert>;

  const Component = evaluated.component;
  if (!Component) return null;

  let mount = generation;
  if (evaluated.inPlace) mount = element.id;

  return (
    <div className={`${element.display}-custom flex flex-col flex-grow`}>
      <ErrorBoundary key={mount}>
        <Component />
      </ErrorBoundary>
    </div>
  );
});

export default CustomElement;
