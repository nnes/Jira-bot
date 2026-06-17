import logging
from typing import Optional

from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, TurnContext
from botframework.connector.auth import CredentialProvider

from app.config import settings

logger = logging.getLogger(__name__)


class _NoAuthCredentialProvider(CredentialProvider):
    """Credential provider that always reports authentication as disabled.

    Used in EMULATOR_MODE=true so the Bot Framework Emulator can connect without
    sending real JWT tokens, even when TEAMS_BOT_APP_ID is set in .env.
    Never use this in production.
    """

    async def is_valid_appid(self, app_id: str) -> bool:
        return True

    async def get_app_password(self, app_id: str) -> Optional[str]:
        return None

    async def is_authentication_disabled(self) -> bool:
        return True


if settings.emulator_mode:
    logger.warning(
        "EMULATOR_MODE=true — JWT auth disabled. Do NOT use this setting in production."
    )
    _credential_provider: Optional[CredentialProvider] = _NoAuthCredentialProvider()
else:
    _credential_provider = None  # use BotFrameworkAdapterSettings default

_adapter_settings = BotFrameworkAdapterSettings(
    app_id=settings.teams_bot_app_id or None,
    app_password=settings.teams_bot_app_password or None,
    channel_auth_tenant=settings.teams_bot_tenant_id or None,
)
adapter = BotFrameworkAdapter(_adapter_settings)

# BotFrameworkAdapter.__init__ unconditionally creates its own SimpleCredentialProvider
# from settings.app_id, ignoring settings.credential_provider.  Override it here so
# the EMULATOR_MODE no-auth provider actually takes effect.
if _credential_provider is not None:
    adapter._credential_provider = _credential_provider


async def on_error(turn_context: TurnContext, error: Exception) -> None:
    logger.error("Bot adapter error: %s", error, exc_info=True)
    try:
        await turn_context.send_activity("Đã xảy ra lỗi. Vui lòng thử lại.")
    except Exception as send_err:
        logger.error("Failed to send error reply: %s", send_err)


adapter.on_turn_error = on_error
