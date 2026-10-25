import { FileSpec, useConfig } from '@chainlit/react-client';

import { Translator } from '@/components/i18n';
import { PaperClip } from '@/components/icons/PaperClip';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger
} from '@/components/ui/tooltip';

import { useUpload } from '@/hooks/useUpload';
import { useTranslation } from 'components/i18n/Translator';

import { ComposerIconButton } from './ComposerIconButton';

interface UploadButtonProps {
  disabled?: boolean;
  fileSpec: FileSpec;
  onFileUpload: (files: File[]) => void;
  onFileUploadError: (error: string) => void;
}

export const UploadButton = ({
  disabled = false,
  fileSpec,
  onFileUpload,
  onFileUploadError
}: UploadButtonProps) => {
  const { config } = useConfig();
  const { t } = useTranslation();
  const upload = useUpload({
    spec: fileSpec,
    onResolved: (payloads: File[]) => onFileUpload(payloads),
    onError: onFileUploadError,
    // окно выбора через input: его открывает и перехватывает любой браузер
    options: { noDrag: true, useFsAccessApi: false }
  });

  if (!upload) return null;
  const { getRootProps, getInputProps } = upload;

  if (!config?.features.spontaneous_file_upload?.enabled) return null;

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-block">
            <input
              id="upload-button-input"
              className="hidden"
              {...getInputProps()}
            />
            <ComposerIconButton
              {...getRootProps()}
              id={disabled ? 'upload-button-loading' : 'upload-button'}
              label={t('chat.input.actions.attachFiles')}
              disabled={disabled}
            >
              <PaperClip />
            </ComposerIconButton>
          </span>
        </TooltipTrigger>
        <TooltipContent>
          <p>
            <Translator path="chat.input.actions.attachFiles" />
          </p>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
};

export default UploadButton;
