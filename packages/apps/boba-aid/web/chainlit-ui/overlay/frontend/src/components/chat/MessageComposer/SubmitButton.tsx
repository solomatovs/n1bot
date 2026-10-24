import { useRecoilValue } from 'recoil';

import {
  historyLoadingState,
  useChatData,
  useChatInteract,
  useChatMessages
} from '@chainlit/react-client';

import { Send } from '@/components/icons/Send';
import { Stop } from '@/components/icons/Stop';
import { Button } from '@/components/ui/button';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger
} from '@/components/ui/tooltip';
import { Translator } from 'components/i18n';

interface SubmitButtonProps {
  disabled?: boolean;
  empty: boolean;
  onSubmit: () => void;
}

export default function SubmitButton({
  disabled,
  empty,
  onSubmit
}: SubmitButtonProps) {
  const { loading } = useChatData();
  const { firstInteraction } = useChatMessages();
  const { stopTask } = useChatInteract();
  // boba: пока воспроизводится история треда, хода нет: вместо «Стоп» —
  // неактивная кнопка отправки (её гасит disabled из useChatData)
  const historyLoading = useRecoilValue(historyLoadingState);

  return (
    <TooltipProvider>
      {/* boba: при идущем ходе Stop — только у пустого поля, с текстом — Send в очередь */}
      {loading && firstInteraction && !historyLoading && empty ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              id="stop-button"
              onClick={stopTask}
              size="icon"
              className="rounded-full h-8 w-8"
            >
              <Stop className="!size-6" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            <p>
              <Translator path="chat.input.actions.stop" />
            </p>
          </TooltipContent>
        </Tooltip>
      ) : (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              id="chat-submit"
              disabled={disabled}
              onClick={onSubmit}
              size="icon"
              className="rounded-full h-8 w-8"
            >
              <Send className="!size-6" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            <p>
              <Translator path="chat.input.actions.send" />
            </p>
          </TooltipContent>
        </Tooltip>
      )}
    </TooltipProvider>
  );
}
