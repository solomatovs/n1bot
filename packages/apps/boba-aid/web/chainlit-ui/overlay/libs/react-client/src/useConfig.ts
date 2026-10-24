import { useEffect, useRef } from 'react';
import { useRecoilState, useRecoilValue } from 'recoil';

import { useApi, useAuth } from './api';
import { chatProfileState, configState } from './state';
import { IChainlitConfig } from './types';

const useConfig = () => {
  const [config, setConfig] = useRecoilState(configState);
  const { isAuthenticated } = useAuth();
  const chatProfile = useRecoilValue(chatProfileState);
  const language = navigator.language || 'en-US';
  // boba: для какого профиля загружен config. Смена профиля (в том числе
  // выравнивание под профиль открытого треда) перечитывает config, но не
  // сбрасывает его: сброс снимал всё приложение со страницы и заново
  // подключал сокет — тред воспроизводился второй раз
  const loadedFor = useRef<string | undefined>(undefined);

  // Build the API URL with optional chat profile parameter
  const apiUrl = isAuthenticated
    ? `/project/settings?language=${language}${chatProfile ? `&chat_profile=${encodeURIComponent(chatProfile)}` : ''}`
    : null;

  const stale = !config || loadedFor.current !== chatProfile;
  const shouldFetch = isAuthenticated && stale;

  const { data, error, isLoading } = useApi<IChainlitConfig>(
    shouldFetch ? apiUrl : null
  );

  useEffect(() => {
    if (!data) return;
    loadedFor.current = chatProfile;
    setConfig(data);
  }, [data, setConfig]);

  return { config, error, isLoading, language };
};

export { useConfig };
