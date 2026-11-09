"""Config flow: paste the auth0 session cookie once."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import OriginEnergyApiClient, OriginEnergyApiError, pick_service
from .auth import (
    OriginAuthConfigError,
    OriginAuthError,
    OriginAuthTransientError,
    async_refresh_access_token,
    clean_cookie,
)
from .const import (
    CONF_AGREEMENT_ID,
    CONF_COOKIE,
    CONF_NMI,
    CONF_SCAN_HOURS,
    DEFAULT_SCAN_HOURS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

COOKIE_SCHEMA = vol.Schema({vol.Required(CONF_COOKIE): str})


class NoServiceError(Exception):
    """No electricity service found on the account."""


class OriginConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _validate(self, raw_cookie: str) -> dict[str, Any]:
        """Log in with the cookie and find the live electricity agreement."""
        session = async_create_clientsession(
            self.hass, cookie_jar=aiohttp.CookieJar()
        )
        auth = await async_refresh_access_token(session, raw_cookie)
        client = OriginEnergyApiClient(session, auth.access_token)
        service = pick_service(await client.async_get_services())
        if service is None:
            raise NoServiceError
        return {
            "cookie": auth.cookie,
            "agreement_id": service["agreementId"],
            "nmi": (service.get("meterPoint") or {}).get("identifier", ""),
        }

    @staticmethod
    def _error_for(err: Exception) -> str:
        if isinstance(err, OriginAuthConfigError):
            return "auth_not_configured"
        if isinstance(err, OriginAuthError):
            return "invalid_cookie"
        if isinstance(err, NoServiceError):
            return "no_service"
        return "cannot_connect"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await self._validate(clean_cookie(user_input[CONF_COOKIE]))
            except (
                OriginAuthConfigError,
                OriginAuthError,
                OriginAuthTransientError,
                OriginEnergyApiError,
                NoServiceError,
            ) as err:
                _LOGGER.debug("Setup validation failed: %s", err)
                errors["base"] = self._error_for(err)
            else:
                await self.async_set_unique_id(info["agreement_id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Origin Energy ({info['nmi'] or info['agreement_id']})",
                    data={
                        CONF_COOKIE: info["cookie"],
                        CONF_AGREEMENT_ID: info["agreement_id"],
                        CONF_NMI: info["nmi"],
                    },
                )
        return self.async_show_form(
            step_id="user", data_schema=COOKIE_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                info = await self._validate(clean_cookie(user_input[CONF_COOKIE]))
            except (
                OriginAuthConfigError,
                OriginAuthError,
                OriginAuthTransientError,
                OriginEnergyApiError,
                NoServiceError,
            ) as err:
                errors["base"] = self._error_for(err)
            else:
                if info["agreement_id"] != entry.data[CONF_AGREEMENT_ID]:
                    errors["base"] = "wrong_account"
                else:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_COOKIE: info["cookie"]}
                    )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=COOKIE_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_SCAN_HOURS, DEFAULT_SCAN_HOURS)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_HOURS, default=current): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=24)
                    )
                }
            ),
        )
