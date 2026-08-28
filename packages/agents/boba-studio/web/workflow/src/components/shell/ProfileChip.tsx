import { UserRound } from "lucide-react";
import { type ReactElement, useCallback } from "react";

import { useServices } from "../../app";
import { useLoadable } from "../../hooks/useLoadable";
import type { Me, ProfileView } from "../../model/account";

type Loaded = {
  me: Me;
  profiles: ProfileView[];
};

/** Чип профиля в топбаре: кто вошёл и выбор профиля из видимых ролям. */
export function ProfileChip(): ReactElement | null {
  const { api, chooseProfile } = useServices();
  const [state] = useLoadable(
    useCallback(async (): Promise<Loaded> => {
      const [me, profiles] = await Promise.all([api.me(), api.profiles()]);
      return { me, profiles };
    }, [api]),
  );

  if (state.kind !== "ready") {
    return null;
  }

  const { me, profiles } = state.value;
  return (
    <label className="topbar__pill profile-chip" title={`signed in as ${me.login}`}>
      <UserRound size={14} />
      <span className="profile-chip__login">{me.login}</span>
      <select
        className="profile-chip__select"
        aria-label="profile"
        value={me.profile}
        onChange={(event) => {
          chooseProfile(event.target.value);
        }}
      >
        {profiles.map((profile) => (
          <option key={profile.name} value={profile.name}>
            {profile.display_name}
          </option>
        ))}
      </select>
    </label>
  );
}
