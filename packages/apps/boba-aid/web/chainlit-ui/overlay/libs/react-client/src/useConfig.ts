import { useEffect, useRef } from 'react';
import { useRecoilState, useRecoilValue } from 'recoil';

import { useApi, useAuth } from './api';
import { chatProfileState, configState } from './state';
import { IChainlitConfig } from './types';

// Подсказка входа влияет только на выбор по умолчанию; права приходят от сервера.
const preferredProfile = (): string => {
  const cookie = document.cookie
    .split('; ')
    .find((value) => value.startsWith('boba_chat_profile='));
  if (!cookie) return '';
  try {
    return decodeURIComponent(cookie.slice('boba_chat_profile='.length));
  } catch {
    return '';
  }
};

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
    const preferred = preferredProfile();
    const profiles = data.chatProfiles ?? [];
    const granted = profiles.some((profile) => profile.name === preferred);
    setConfig(
      granted
        ? {
            ...data,
            chatProfiles: profiles.map((profile) => ({
              ...profile,
              default: profile.name === preferred
            }))
          }
        : data
    );
  }, [data, setConfig]);

  return { config, error, isLoading, language };
};

export { useConfig };
