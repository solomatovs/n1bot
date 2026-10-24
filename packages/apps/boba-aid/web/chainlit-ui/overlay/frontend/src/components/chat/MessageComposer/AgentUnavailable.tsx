import { useContext, useState } from 'react';
import { useRecoilValue } from 'recoil';
import { v4 as uuidv4 } from 'uuid';

import {
  ChainlitContext,
  agentStateState,
  sessionIdState
} from '@chainlit/react-client';

import { Button } from '@/components/ui/button';
import { useTranslation } from 'components/i18n/Translator';

// boba: агент вкладки недоступен — полоса над полем ввода с текстом сбоя от
// сервера aid и кнопкой повтора; поле ввода заперто (useChatData), пока
// сервер не сообщит, что агент снова отвечает
const RETRY_ACTION = 'agent_retry';

export default function AgentUnavailable() {
  const apiClient = useContext(ChainlitContext);
  const sessionId = useRecoilValue(sessionIdState);
  const agentState = useRecoilValue(agentStateState);
  const [retrying, setRetrying] = useState(false);
  const { t } = useTranslation();

  if (agentState.available) return null;

  const retry = async () => {
    setRetrying(true);
    try {
      await apiClient.callAction(
        {
          id: uuidv4(),
          forId: '',
          name: RETRY_ACTION,
          label: '',
          tooltip: '',
          payload: {},
          onClick: () => undefined
        },
        sessionId
      );
    } finally {
      setRetrying(false);
    }
  };

  return (
    <div
      role="alert"
      data-agent-unavailable=""
      className="mb-2 flex items-start gap-3 rounded-2xl border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-foreground"
    >
      <span className="flex-grow break-words">{agentState.text}</span>
      <Button
        id="agent-retry"
        size="sm"
        variant="outline"
        disabled={retrying}
        onClick={retry}
      >
        {t('aid.agent.retry')}
      </Button>
    </div>
  );
}
