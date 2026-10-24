import { cn } from '@/lib/utils';
import type { ReactNode } from 'react';

// boba: пометка агента (notice ACP), ошибка хода и полоса недоступного агента
// — один блок: рамка и лёгкий фон цвета важности (токены Consta из ui.css),
// внутри текст человеку, мелкая строка причины и кнопки, если они есть
export type NoticeSeverity = 'info' | 'warning' | 'error';

const SEVERITIES: readonly NoticeSeverity[] = ['info', 'warning', 'error'];

export function noticeSeverity(value: unknown): NoticeSeverity {
  const known = SEVERITIES.find((severity) => severity === value);
  return known ?? 'info';
}

interface Props {
  severity: NoticeSeverity;
  text: string;
  detail?: string;
  role?: 'status' | 'alert';
  className?: string;
  children?: ReactNode;
}

export function AgentNotice({
  severity,
  text,
  detail,
  role = 'status',
  className,
  children
}: Props) {
  return (
    <div
      role={role}
      data-agent-notice={severity}
      className={cn('agent-notice', className)}
    >
      <div className="agent-notice-body">
        <div className="agent-notice-text">{text}</div>
        {detail ? <div className="agent-notice-detail">{detail}</div> : null}
      </div>
      {children}
    </div>
  );
}
