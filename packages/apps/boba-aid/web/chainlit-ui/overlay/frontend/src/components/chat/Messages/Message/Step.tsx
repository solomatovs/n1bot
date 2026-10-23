import { cn } from '@/lib/utils';
import {
  PropsWithChildren,
  ReactNode,
  useEffect,
  useMemo,
  useState
} from 'react';

import type { IStep } from '@chainlit/react-client';

import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger
} from '@/components/ui/accordion';
import { Translator } from 'components/i18n';

interface Props {
  step: IStep;
  isRunning?: boolean;
}

export default function Step({
  step,
  children,
  isRunning
}: PropsWithChildren<Props>) {
  const using = useMemo(() => {
    return isRunning && step.start && !step.end && !step.isError;
  }, [step, isRunning]);

  const hasContent = step.input || step.output || step.steps?.length;
  const isError = step.isError;
  const stepName = step.name;
  // boba: подпись контейнера хода — текущее действие хода; длинная обрезается
  // по ширине ленты, целиком — во всплывающей подсказке
  let label: ReactNode = stepName;
  if (step.type === 'run') {
    label = (
      <span className="truncate min-w-0" title={stepName}>
        {stepName}
      </span>
    );
  }

  const [openValue, setOpenValue] = useState<string>(
    step.defaultOpen ? step.id : ''
  );

  // Auto-collapse when step finishes if autoCollapse is set
  useEffect(() => {
    if (!using && step.autoCollapse) {
      setOpenValue('');
    }
  }, [using, step.autoCollapse]);

  // If there's no content, just render the status without accordion
  if (!hasContent) {
    return (
      <div className="flex flex-col flex-grow w-0">
        <p
          className={cn(
            'flex items-center gap-1 font-medium',
            isError && 'text-red-500',
            // boba: приглушённый цвет законченного шага перекрывал красный у
            // провала — tailwind-merge оставляет последний класс цвета
            !using && !isError && 'text-muted-foreground',
            using && 'loading-shimmer'
          )}
          id={`step-${stepName}`}
        >
          {using ? (
            <>
              <Translator path="chat.messages.status.using" /> {label}
            </>
          ) : (
            <>
              <Translator path="chat.messages.status.used" /> {label}
            </>
          )}
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col flex-grow w-0">
      <Accordion
        type="single"
        collapsible
        value={openValue}
        onValueChange={(val) => setOpenValue(val)}
        className="w-full"
      >
        <AccordionItem value={step.id} className="border-none">
          <AccordionTrigger
            className={cn(
              'flex items-center gap-1 justify-start transition-none p-0 hover:no-underline min-w-0',
              isError && 'text-red-500',
              !using && !isError && 'text-muted-foreground hover:text-foreground',
              using && 'loading-shimmer'
            )}
            id={`step-${stepName}`}
          >
            {using ? (
              <>
                <Translator path="chat.messages.status.using" /> {label}
              </>
            ) : (
              <>
                <Translator path="chat.messages.status.used" /> {label}
              </>
            )}
          </AccordionTrigger>
          <AccordionContent>
            <div className="flex-grow mt-4 ml-1 pl-4 border-l-2 border-primary">
              {children}
            </div>
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}
