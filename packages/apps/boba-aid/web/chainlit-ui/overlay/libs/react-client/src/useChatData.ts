import { useRecoilValue } from 'recoil';

import {
  actionState,
  askUserState,
  callFnState,
  chatSettingsDefaultValueSelector,
  chatSettingsInputsState,
  chatSettingsValueState,
  elementState,
  loadingState,
  sessionState,
  tasklistState
} from './state';
import { agentStateState, historyLoadingState } from './useChatSession';

export interface IToken {
  id: number | string;
  token: string;
  isSequence: boolean;
  isInput: boolean;
}

const useChatData = () => {
  const loading = useRecoilValue(loadingState);
  // boba: пока воспроизводится история треда, отправить вопрос нельзя
  const historyLoading = useRecoilValue(historyLoadingState);
  // boba: недоступный агент запирает поле ввода до повтора
  const agentState = useRecoilValue(agentStateState);
  const elements = useRecoilValue(elementState);
  const tasklists = useRecoilValue(tasklistState);
  const actions = useRecoilValue(actionState);
  const session = useRecoilValue(sessionState);
  const askUser = useRecoilValue(askUserState);
  const callFn = useRecoilValue(callFnState);
  const chatSettingsInputs = useRecoilValue(chatSettingsInputsState);
  const chatSettingsValue = useRecoilValue(chatSettingsValueState);
  const chatSettingsDefaultValue = useRecoilValue(
    chatSettingsDefaultValueSelector
  );

  const connected = session?.socket.connected && !session?.error;
  // boba: идущий ход поле ввода не запирает — сообщение уходит в очередь агента
  const disabled =
    !connected ||
    historyLoading ||
    !agentState.available ||
    askUser?.spec.type === 'file' ||
    askUser?.spec.type === 'action' ||
    askUser?.spec.type === 'element';

  return {
    actions,
    askUser,
    callFn,
    chatSettingsDefaultValue,
    chatSettingsInputs,
    chatSettingsValue,
    connected,
    disabled,
    elements,
    error: session?.error,
    loading,
    tasklists
  };
};

export { useChatData };
