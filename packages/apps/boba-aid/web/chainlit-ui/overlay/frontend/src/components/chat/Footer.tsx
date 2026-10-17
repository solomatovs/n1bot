import { cn, hasMessage } from '@/lib/utils';
import { MessageContext, defaultMessageContext } from 'contexts/MessageContext';
import { MutableRefObject, useEffect, useMemo, useState } from 'react';

import {
  FileSpec,
  ICustomElement,
  IMessageElement,
  useChatData,
  useChatMessages
} from '@chainlit/react-client';

import CustomElement from '@/components/Elements/CustomElement';
import WaterMark from '@/components/WaterMark';

import MessageComposer from './MessageComposer';

// boba: вопрос агента (ask элементом Question) встаёт вместо поля ввода:
// поле скрыто, а не снято — черновик остаётся. Пока пользователь печатает
// (поле с текстом правили меньше HOLD_MS назад), панель придержана, над полем
// подсказка.
const HOLD_MS = 1500;
const QUESTION = 'Question';

interface Props {
  fileSpec: FileSpec;
  onFileUpload: (payload: File[]) => void;
  onFileUploadError: (error: string) => void;
  autoScrollRef: MutableRefObject<boolean>;
  showIfEmptyThread?: boolean;
}

const isQuestion = (
  element: IMessageElement,
  id: string | undefined
): element is ICustomElement =>
  element.type === 'custom' && element.name === QUESTION && element.id === id;

export default function ChatFooter({ showIfEmptyThread, ...props }: Props) {
  const { messages } = useChatMessages();
  const { askUser, elements } = useChatData();
  const [typedAt, setTypedAt] = useState(0);
  const [drafted, setDrafted] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [composer, setComposer] = useState<HTMLDivElement | null>(null);

  const question = useMemo(() => {
    if (askUser?.spec.type !== 'element') return undefined;
    const id = askUser.spec.element_id;
    return elements.find((element): element is ICustomElement =>
      isQuestion(element, id)
    );
  }, [askUser, elements]);

  const docked = useMemo(() => {
    if (!question) return undefined;
    return { ...question, props: { ...question.props, docked: true } };
  }, [question]);

  const context = useMemo(
    () => ({ ...defaultMessageContext, askUser }),
    [askUser]
  );

  const held = Boolean(question) && drafted && now - typedAt < HOLD_MS;

  useEffect(() => {
    if (!held) return;
    const timer = setTimeout(
      () => setNow(Date.now()),
      HOLD_MS - (now - typedAt) + 10
    );
    return () => clearTimeout(timer);
  }, [held, now, typedAt]);

  useEffect(() => {
    if (question) setNow(Date.now());
  }, [question]);

  useEffect(() => {
    if (!composer) return;
    // состояние меняется после разбора события: перерисовка посреди него
    // вернула бы управляемому полю прежнее значение и съела ввод
    const typed = (event: Event) => {
      const target = event.target as HTMLTextAreaElement | HTMLElement;
      let text = target.textContent || '';
      if ('value' in target) text = target.value;
      const at = Date.now();
      window.setTimeout(() => {
        setDrafted(text.trim() !== '');
        setTypedAt(at);
        setNow(at);
      }, 0);
    };
    composer.addEventListener('input', typed);
    return () => composer.removeEventListener('input', typed);
  }, [composer]);

  if (!hasMessage(messages) && !showIfEmptyThread) return null;

  const shown = Boolean(docked) && !held;

  return (
    <div className={cn('relative flex flex-col items-center gap-2 w-full')}>
      {held ? (
        <div className="q-root w-full" data-question-held>
          <div className="q-hint">Waiting for permission…</div>
        </div>
      ) : null}
      {shown && docked ? (
        <MessageContext.Provider value={context}>
          <div className="w-full" id="question-dock">
            <CustomElement element={docked} />
          </div>
        </MessageContext.Provider>
      ) : null}
      <div
        ref={setComposer}
        className="w-full"
        style={shown ? { display: 'none' } : undefined}
      >
        <MessageComposer {...props} />
      </div>
      <WaterMark />
    </div>
  );
}
