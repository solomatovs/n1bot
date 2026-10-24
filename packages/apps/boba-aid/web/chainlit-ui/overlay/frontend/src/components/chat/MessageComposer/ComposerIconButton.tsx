import { cn } from '@/lib/utils';
import { forwardRef } from 'react';

import { Button, type ButtonProps } from '@/components/ui/button';

// Кнопка-иконка нижней строки поля ввода: размер кнопки и иконки задают
// токены --composer-button-size и --composer-icon-size в public/ui.css;
// подпись — aria-label, видимая подсказка — у владельца кнопки.
interface ComposerIconButtonProps extends ButtonProps {
  label: string;
}

export const ComposerIconButton = forwardRef<
  HTMLButtonElement,
  ComposerIconButtonProps
>(({ label, className, ...props }, ref) => (
  <Button
    ref={ref}
    variant="ghost"
    size="icon"
    aria-label={label}
    className={cn('composer-icon-button hover:bg-muted rounded-full', className)}
    {...props}
  />
));

ComposerIconButton.displayName = 'ComposerIconButton';

export default ComposerIconButton;
