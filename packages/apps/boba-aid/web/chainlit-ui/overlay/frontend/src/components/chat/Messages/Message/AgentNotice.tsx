import { cn } from '@/lib/utils';
import type { ReactNode } from 'react';

// boba: пометка агента (notice ACP), ошибка хода и полоса недоступного агента
// — одна строка ленты: в колонке аватара мелкая точка цвета важности (токены
// Consta из ui.css), справа обычный текст человеку и мелкая строка причины
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
      <span className="agent-notice-mark" aria-hidden="true">
        <span className="agent-notice-dot" />
      </span>
      <div className="agent-notice-body">
        <div className="agent-notice-text">{text}</div>
        {detail ? <div className="agent-notice-detail">{detail}</div> : null}
      </div>
      {children}
    </div>
  );
}
