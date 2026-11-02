"""Config flow: login + password for the lk.roscomsys.ru personal account."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import SksAuthError, SksClient, SksError
from .const import (
    CONF_ACCOUNT_ID,
    CONF_LOGIN,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    MIN_SCAN_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_LOGIN): str,
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

_SCAN_INTERVAL_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_SCAN_INTERVAL_MINUTES,
        max=1440,
        step=1,
        unit_of_measurement="min",
        mode=NumberSelectorMode.BOX,
    )
)


class SksConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config Flow для интеграции SKS."""

    VERSION = 1

    def __init__(self) -> None:
        self._login = ""
        self._password = ""
        self._account_ids: list[str] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SksOptionsFlow:
        return SksOptionsFlow(config_entry)

    async def _validate(
        self, login: str, password: str
    ) -> tuple[list[str], str | None]:
        """Sign in; return (account ids bound to the login, error_key)."""
        # Own session + cookie jar; closed right after validation.
        session = async_create_clientsession(self.hass, auto_cleanup=False)
        try:
            client = SksClient(session, login, password)
            await client.async_sign_in()
            account_ids = await client.async_get_account_ids()
        except SksAuthError:
            return [], "invalid_auth"
        except SksError as err:
            _LOGGER.debug("Validation failed: %s", err)
            return [], "cannot_connect"
        finally:
            await session.close()
        return account_ids, None

    async def _create(self, account_id: str | None) -> ConfigFlowResult:
        await self.async_set_unique_id(account_id or self._login.lower())
        self._abort_if_unique_id_configured()
        data: dict[str, Any] = {
            CONF_LOGIN: self._login,
            CONF_PASSWORD: self._password,
        }
        if account_id:
            data[CONF_ACCOUNT_ID] = account_id
        return self.async_create_entry(
            title=f"СКС {account_id or self._login}",
            data=data,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Шаг ввода логина и пароля личного кабинета."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._login = user_input[CONF_LOGIN].strip()
            self._password = user_input[CONF_PASSWORD]
            account_ids, error = await self._validate(
                self._login, self._password
            )
            if error:
                errors["base"] = error
            elif len(account_ids) > 1:
                # Several ЛС on one login — let the user pick; a second entry
                # can be added later for another account.
                self._account_ids = account_ids
                return await self.async_step_account()
            else:
                return await self._create(
                    account_ids[0] if account_ids else None
                )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Шаг выбора лицевого счёта, когда их несколько на один логин."""
        if user_input is not None:
            return await self._create(user_input[CONF_ACCOUNT_ID])
        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACCOUNT_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=self._account_ids,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-enter the password — the stored one no longer signs in."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            login = user_input[CONF_LOGIN].strip()
            _account_ids, error = await self._validate(
                login, user_input[CONF_PASSWORD]
            )
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_LOGIN: login,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA,
                {CONF_LOGIN: entry.data.get(CONF_LOGIN, "")},
            ),
            errors=errors,
        )


class SksOptionsFlow(OptionsFlow):
    """Scan interval option."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self._config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=current
                    ): _SCAN_INTERVAL_SELECTOR
                }
            ),
        )
