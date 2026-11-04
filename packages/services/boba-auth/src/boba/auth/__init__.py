"""Общие сценарии входа и подтверждения сессии, без транспортных адаптеров."""

from boba.auth.service import SessionService
from boba.auth.signin import SignInProviders, SignIns

__all__ = ["SessionService", "SignInProviders", "SignIns"]
