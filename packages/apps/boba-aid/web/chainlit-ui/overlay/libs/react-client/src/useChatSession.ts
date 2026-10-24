import { debounce } from 'lodash';
import { useCallback, useContext, useEffect, useMemo, useRef } from 'react';
import {
  atom,
  useRecoilState,
  useRecoilValue,
  useResetRecoilState,
  useSetRecoilState
} from 'recoil';
import io from 'socket.io-client';
import { toast } from 'sonner';
import {
  actionState,
  askUserState,
  audioConnectionState,
  callFnState,
  chatProfileState,
  chatSettingsInputsState,
  chatSettingsValueState,
  commandsState,
  currentThreadIdState,
  elementState,
  favoriteMessagesState,
  firstUserInteraction,
  isAiSpeakingState,
  loadingState,
  mcpState,
  messagesState,
  modesState,
  resumeThreadErrorState,
  sessionIdState,
  sessionState,
  sideViewState,
  tasklistState,
  threadHistoryState,
  threadIdToResumeState,
  tokenCountState,
  wavRecorderState,
  wavStreamPlayerState
} from 'src/state';
import {
  IAction,
  ICommand,
  IElement,
  IMessageElement,
  IMode,
  IStep,
  ITasklistElement,
  IThread,
  ThreadHistory
} from 'src/types';
import { StepOp, addMessage, applyStepOps } from 'src/utils/message';

import { OutputAudioChunk } from './types/audio';

import { ChainlitContext } from './context';
import type { IToken } from './useChatData';

// boba: в скрытой вкладке requestAnimationFrame не идёт — пачка уходит таймером
const STEP_FLUSH_HIDDEN_MS = 100;

// boba: имена событий сокета, которые шлёт сервер aid сверх событий chainlit
const HISTORY_LOADED_EVENT = 'history_loaded';

// boba: вкладка открыла тред, и его история ещё воспроизводится: лента
// показывает загрузку, кнопка отправки неактивна (а не «Стоп» хода)
// boba: после каждой записи истории агента сервер шлёт время активности
// треда из базы; панель тредов ставит тред по нему, даже закрытая (лист на
// узком окне). Тред вне загруженных страниц — перечитывание первой страницы:
// ThreadHistory загружает список, когда threads нет
const THREAD_ACTIVITY = 'boba:thread-activity';

interface ThreadActivity {
  type: typeof THREAD_ACTIVITY;
  threadId: string;
  updatedAt: string;
}

const isThreadActivity = (data: unknown): data is ThreadActivity =>
  typeof data === 'object' &&
  data !== null &&
  (data as { type?: unknown }).type === THREAD_ACTIVITY;

const withActivity = (
  history: ThreadHistory | undefined,
  activity: ThreadActivity
): ThreadHistory | undefined => {
  const threads = history?.threads;
  if (!threads) return history;
  const index = threads.findIndex((thread) => thread.id === activity.threadId);
  if (index === -1) {
    return { ...history, threads: undefined, pageInfo: undefined };
  }
  const thread = threads[index];
  if (Date.parse(activity.updatedAt) <= Date.parse(String(thread.updatedAt))) {
    return history;
  }
  const moved = [...threads];
  moved[index] = { ...thread, updatedAt: activity.updatedAt };
  return { ...history, threads: moved };
};

export const historyLoadingState = atom<boolean>({
  key: 'BobaHistoryLoading',
  default: false
});

// boba: доступен ли агент вкладки (событие сокета agent_state сервера aid):
// недоступный запирает поле ввода полосой с текстом сбоя и кнопкой повтора;
// key — ключ перевода слов человеку (aid.failure.*), detail — подробность
const AGENT_STATE_EVENT = 'agent_state';

export interface IAgentState {
  available: boolean;
  key: string;
  detail: string;
}

export const agentStateState = atom<IAgentState>({
  key: 'BobaAgentState',
  default: { available: true, key: '', detail: '' }
});

const useChatSession = () => {
  const client = useContext(ChainlitContext);
  const sessionId = useRecoilValue(sessionIdState);

  const [session, setSession] = useRecoilState(sessionState);
  const setIsAiSpeaking = useSetRecoilState(isAiSpeakingState);
  const setAudioConnection = useSetRecoilState(audioConnectionState);
  const resetChatSettingsValue = useResetRecoilState(chatSettingsValueState);
  const setChatSettingsValue = useSetRecoilState(chatSettingsValueState);
  const setFirstUserInteraction = useSetRecoilState(firstUserInteraction);
  const setThreadHistory = useSetRecoilState(threadHistoryState);
  const setLoading = useSetRecoilState(loadingState);
  const setMcps = useSetRecoilState(mcpState);
  const wavStreamPlayer = useRecoilValue(wavStreamPlayerState);
  const wavRecorder = useRecoilValue(wavRecorderState);
  const setMessages = useSetRecoilState(messagesState);
  const setAskUser = useSetRecoilState(askUserState);
  const setCallFn = useSetRecoilState(callFnState);
  const setCommands = useSetRecoilState(commandsState);
  const setModes = useSetRecoilState(modesState);
  const setSideView = useSetRecoilState(sideViewState);
  const setElements = useSetRecoilState(elementState);
  const setTasklists = useSetRecoilState(tasklistState);
  const setActions = useSetRecoilState(actionState);
  const setChatSettingsInputs = useSetRecoilState(chatSettingsInputsState);
  const setTokenCount = useSetRecoilState(tokenCountState);
  const [chatProfile, setChatProfile] = useRecoilState(chatProfileState);
  const idToResume = useRecoilValue(threadIdToResumeState);
  const setThreadResumeError = useSetRecoilState(resumeThreadErrorState);
  const setFavoriteMessages = useSetRecoilState(favoriteMessagesState);
  const setHistoryLoading = useSetRecoilState(historyLoadingState);
  const setAgentState = useSetRecoilState(agentStateState);

  const [currentThreadId, setCurrentThreadId] =
    useRecoilState(currentThreadIdState);

  // boba: setMessages на каждое событие ленты (new_message, update_message,
  // stream_token…) перерисовывал её на каждое событие: при воспроизведении
  // истории — сотни перерисовок подряд. События копятся и применяются одним
  // обновлением раз в кадр отрисовки в порядке прихода; остальные события
  // сокета сначала сбрасывают пачку
  const pendingSteps = useRef<StepOp[]>([]);
  const stepFlushScheduled = useRef(false);

  const flushSteps = useCallback(() => {
    stepFlushScheduled.current = false;
    const batch = pendingSteps.current;
    if (!batch.length) return;
    pendingSteps.current = [];
    setMessages((oldMessages) => applyStepOps(oldMessages, batch));
  }, [setMessages]);

  const queueStep = useCallback(
    (op: StepOp) => {
      pendingSteps.current.push(op);
      if (stepFlushScheduled.current) return;
      stepFlushScheduled.current = true;
      if (typeof document !== 'undefined' && document.hidden) {
        setTimeout(flushSteps, STEP_FLUSH_HIDDEN_MS);
      } else {
        requestAnimationFrame(flushSteps);
      }
    },
    [flushSteps]
  );

  // boba: профиль треда, выставленный после resume_thread, попадает и в auth
  // живого сокета: переподключение socket.io после обрыва сети восстанавливает
  // тред с его профилем, а не с тем, что был до открытия треда
  useEffect(() => {
    if (session?.socket) {
      session.socket.auth['chatProfile'] = chatProfile
        ? encodeURIComponent(chatProfile)
        : '';
    }
  }, [chatProfile]);

  // Use currentThreadId as thread id in websocket header
  useEffect(() => {
    if (session?.socket) {
      session.socket.auth['threadId'] = currentThreadId || '';
    }
  }, [currentThreadId]);

  // boba: App зовёт connect на каждое изменение своих зависимостей — и когда
  // профиль треда из resume_thread перезагружает конфиг. Сессию этого треда
  // сервер уже восстановил с профилем треда, поэтому живой сокет той же сессии
  // и того же треда не пересоздаётся; clear() меняет sessionId — сокет новый
  const live = useRef<{ key: string; socket: ReturnType<typeof io> }>();

  const _connect = useCallback(
    async ({
      transports,
      userEnv
    }: {
      transports?: string[];
      userEnv: Record<string, string>;
    }) => {
      const key = JSON.stringify([sessionId, idToResume || '', userEnv]);
      if (live.current?.key === key && live.current.socket.active) return;

      const { protocol, host, pathname } = new URL(client.httpEndpoint);
      const uri = `${protocol}//${host}`;
      const path =
        pathname && pathname !== '/'
          ? `${pathname}/ws/socket.io`
          : '/ws/socket.io';

      try {
        await client.stickyCookie(sessionId);
      } catch (err) {
        console.error(`Failed to set sticky session cookie: ${err}`);
      }

      // boba: пачка и состояние агента прежнего сокета принадлежат прежнему треду
      pendingSteps.current = [];
      setAgentState({ available: true, key: '', detail: '' });

      const socket = io(uri, {
        path,
        withCredentials: true,
        transports,
        auth: {
          clientType: client.type,
          sessionId,
          threadId: idToResume || '',
          userEnv: JSON.stringify(userEnv),
          chatProfile: chatProfile ? encodeURIComponent(chatProfile) : ''
        }
      });
      live.current = { key, socket };
      setSession((old) => {
        old?.socket?.removeAllListeners();
        old?.socket?.close();
        return {
          socket
        };
      });

      socket.on('connect', () => {
        socket.emit('connection_successful');
        setSession((s) => ({ ...s!, error: false }));
        socket.emit('fetch_favorites');
        setMcps((prev) =>
          prev.map((mcp) => {
            let promise;
            if (mcp.clientType === 'sse') {
              promise = client.connectSseMCP(sessionId, mcp.name, mcp.url!);
            } else if (mcp.clientType === 'streamable-http') {
              promise = client.connectStreamableHttpMCP(
                sessionId,
                mcp.name,
                mcp.url!,
                mcp.headers || {}
              );
            } else {
              promise = client.connectStdioMCP(
                sessionId,
                mcp.name,
                mcp.command!
              );
            }
            promise
              .then(async ({ success, mcp }) => {
                setMcps((prev) =>
                  prev.map((existingMcp) => {
                    if (existingMcp.name === mcp.name) {
                      return {
                        ...existingMcp,
                        status: success ? 'connected' : 'failed',
                        tools: mcp ? mcp.tools : existingMcp.tools
                      };
                    }
                    return existingMcp;
                  })
                );
              })
              .catch(() => {
                setMcps((prev) =>
                  prev.map((existingMcp) => {
                    if (existingMcp.name === mcp.name) {
                      return {
                        ...existingMcp,
                        status: 'failed'
                      };
                    }
                    return existingMcp;
                  })
                );
              });
            return { ...mcp, status: 'connecting' };
          })
        );
      });

      socket.on('connect_error', (_) => {
        setSession((s) => ({ ...s!, error: true }));
      });

      socket.on('task_start', () => {
        setLoading(true);
      });

      socket.on('task_end', () => {
        flushSteps();
        setLoading(false);
      });

      socket.on('reload', () => {
        socket.emit('clear_session');
        window.location.reload();
      });

      socket.on('audio_connection', async (state: 'on' | 'off') => {
        if (state === 'on') {
          let isFirstChunk = true;
          const startTime = Date.now();
          const mimeType = 'pcm16';
          try {
            await wavRecorder.begin();
            await wavStreamPlayer.connect();
            await wavRecorder.record(async (data) => {
              const elapsedTime = Date.now() - startTime;
              socket.emit('audio_chunk', {
                isStart: isFirstChunk,
                mimeType,
                elapsedTime,
                data: data.mono
              });
              isFirstChunk = false;
            });
            wavStreamPlayer.onStop = () => setIsAiSpeaking(false);
          } catch {
            try {
              await wavRecorder.end();
            } catch {
              // ignored
            }
            await wavStreamPlayer.interrupt();
            socket.emit('audio_end');
            setAudioConnection('off');
            return;
          }
        } else {
          await wavRecorder.end();
          await wavStreamPlayer.interrupt();
        }
        setAudioConnection(state);
      });

      socket.on('audio_chunk', (chunk: OutputAudioChunk) => {
        wavStreamPlayer.add16BitPCM(chunk.data, chunk.track);
        setIsAiSpeaking(true);
      });

      socket.on('audio_interrupt', () => {
        wavStreamPlayer.interrupt();
      });

      socket.on('resume_thread', (thread: IThread) => {
        flushSteps();
        const isReadOnlyView = Boolean(
          (thread as any)?.metadata?.viewer_read_only
        );
        if (!isReadOnlyView && idToResume && thread.id !== idToResume) {
          window.location.href = `/thread/${thread.id}`;
        }
        if (!isReadOnlyView && idToResume) {
          setCurrentThreadId(thread.id);
        }
        let messages: IStep[] = [];
        for (const step of thread.steps) {
          messages = addMessage(messages, step);
        }
        if (thread.metadata?.chat_profile) {
          setChatProfile(thread.metadata?.chat_profile);
        }
        if (thread.metadata?.chat_settings) {
          setChatSettingsValue(thread.metadata?.chat_settings);
        }
        setMessages(messages);
        const elements = thread.elements || [];
        setTasklists(
          (elements as ITasklistElement[]).filter((e) => e.type === 'tasklist')
        );
        setElements(
          (elements as IMessageElement[]).filter(
            (e) => ['avatar', 'tasklist'].indexOf(e.type) === -1
          )
        );
      });

      socket.on('resume_thread_error', (error?: string) => {
        setHistoryLoading(false);
        setThreadResumeError(error);
      });

      socket.on(HISTORY_LOADED_EVENT, () => {
        flushSteps();
        setHistoryLoading(false);
      });

      socket.on(AGENT_STATE_EVENT, (state: IAgentState) => {
        flushSteps();
        setAgentState(state);
      });

      socket.on('new_message', (message: IStep) => {
        queueStep({ kind: 'add', step: message });
      });

      socket.on(
        'first_interaction',
        (event: { interaction: string; thread_id: string }) => {
          if (event.interaction === 'resume') {
            setHistoryLoading(true);
          }
          setFirstUserInteraction(event.interaction);
          setCurrentThreadId(event.thread_id);
        }
      );

      socket.on('update_message', (message: IStep) => {
        queueStep({ kind: 'update', step: message });
      });

      socket.on('delete_message', (message: IStep) => {
        queueStep({ kind: 'delete', step: message });
      });

      socket.on('stream_start', (message: IStep) => {
        queueStep({ kind: 'add', step: message });
      });

      socket.on('stream_token', (token: IToken) => {
        queueStep({ kind: 'token', token });
      });

      socket.on('ask', ({ msg, spec }, callback) => {
        flushSteps();
        setAskUser({ spec, callback, parentId: msg.parentId });
        // boba: вопрос элементом показывает панель у поля ввода, а шаг его
        // вызова в ленте уже есть — msg ленту не трогает
        if (spec.type !== 'element') {
          setMessages((oldMessages) => addMessage(oldMessages, msg));
        }

        setLoading(false);
      });

      socket.on('ask_timeout', () => {
        setAskUser(undefined);
        setLoading(false);
      });

      socket.on('clear_ask', () => {
        setAskUser(undefined);
      });

      socket.on('call_fn', ({ name, args }, callback) => {
        setCallFn({ name, args, callback });
      });

      socket.on('clear_call_fn', () => {
        setCallFn(undefined);
      });

      socket.on('call_fn_timeout', () => {
        setCallFn(undefined);
      });

      socket.on('chat_settings', (inputs: any) => {
        setChatSettingsInputs(inputs);
        resetChatSettingsValue();
      });

      socket.on('set_commands', (commands: ICommand[]) => {
        setCommands(commands);
      });

      socket.on('set_modes', (modes: IMode[]) => {
        setModes(modes);
      });

      socket.on('set_favorites', (steps: IStep[]) => {
        setFavoriteMessages(steps);
      });

      socket.on('set_sidebar_title', (title: string) => {
        setSideView((prev) => {
          if (prev?.title === title) return prev;
          return { title, elements: prev?.elements || [] };
        });
      });

      socket.on(
        'set_sidebar_elements',
        ({ elements, key }: { elements: IMessageElement[]; key?: string }) => {
          if (!elements.length) {
            setSideView(undefined);
          } else {
            elements.forEach((element) => {
              if (!element.url && element.chainlitKey) {
                element.url = client.getElementUrl(
                  element.chainlitKey,
                  sessionId
                );
              }
            });
            setSideView((prev) => {
              if (prev?.key === key) return prev;
              return { title: prev?.title || '', elements: elements, key };
            });
          }
        }
      );

      socket.on('element', (element: IElement) => {
        if (!element.url && element.chainlitKey) {
          element.url = client.getElementUrl(element.chainlitKey, sessionId);
        }

        if (element.type === 'tasklist') {
          setTasklists((old) => {
            const index = old.findIndex((e) => e.id === element.id);
            if (index === -1) {
              return [...old, element];
            } else {
              return [...old.slice(0, index), element, ...old.slice(index + 1)];
            }
          });
        } else {
          setElements((old) => {
            const index = old.findIndex((e) => e.id === element.id);
            if (index === -1) {
              return [...old, element];
            } else {
              return [...old.slice(0, index), element, ...old.slice(index + 1)];
            }
          });
        }
      });

      socket.on('remove_element', (remove: { id: string }) => {
        setElements((old) => {
          return old.filter((e) => e.id !== remove.id);
        });
        setTasklists((old) => {
          return old.filter((e) => e.id !== remove.id);
        });
      });

      socket.on('action', (action: IAction) => {
        setActions((old) => [...old, action]);
      });

      socket.on('remove_action', (action: IAction) => {
        setActions((old) => {
          const index = old.findIndex((a) => a.id === action.id);
          if (index === -1) return old;
          return [...old.slice(0, index), ...old.slice(index + 1)];
        });
      });

      socket.on('token_usage', (count: number) => {
        setTokenCount((old) => old + count);
      });

      socket.on('window_message', (data: any) => {
        if (isThreadActivity(data)) {
          setThreadHistory((prev) => withActivity(prev, data));
          return;
        }
        if (window.parent) {
          // Код chainlit: канал окна вьювера. Целевой origin не сужаем —
          // страница встраивается на произвольный хост, а данные несут
          // только имя окна и уже отрисованное содержимое панели.
          // nosemgrep: wildcard-postmessage-configuration
          window.parent.postMessage(data, '*');
        }
      });

      socket.on('toast', (data: { message: string; type: string }) => {
        if (!data.message) {
          console.warn('No message received for toast.');
          return;
        }

        switch (data.type) {
          case 'info':
            toast.info(data.message);
            break;
          case 'error':
            toast.error(data.message);
            break;
          case 'success':
            toast.success(data.message);
            break;
          case 'warning':
            toast.warning(data.message);
            break;
          default:
            toast(data.message);
            break;
        }
      });
    },
    [
      setSession,
      sessionId,
      idToResume,
      chatProfile,
      flushSteps,
      queueStep,
      setAgentState
    ]
  );

  // boba: у каждого _connect свой debounce; отложенный вызов прежнего не
  // отменялся, и после clear() уходили два подключения — первый сокет
  // закрывался вторым, не успев открыться
  const connect = useMemo(() => debounce(_connect, 200), [_connect]);
  useEffect(() => () => connect.cancel(), [connect]);

  const disconnect = useCallback(() => {
    if (session?.socket) {
      session.socket.removeAllListeners();
      session.socket.close();
    }
  }, [session]);

  return {
    connect,
    disconnect,
    session,
    sessionId,
    chatProfile,
    idToResume,
    setChatProfile
  };
};

export { useChatSession };
