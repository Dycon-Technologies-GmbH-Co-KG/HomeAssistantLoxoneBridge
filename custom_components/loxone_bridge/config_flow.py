"""Config flow for Loxone Bridge integration."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_SYNC_HA_TO_LOXONE,
    CONF_SYNC_LOXONE_TO_HA,
    CONF_USE_TLS,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DEFAULT_USE_TLS,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_USERNAME, default="admin"): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_USE_TLS, default=DEFAULT_USE_TLS): bool,
        vol.Required(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
        vol.Required(CONF_SYNC_HA_TO_LOXONE, default=True): bool,
        vol.Required(CONF_SYNC_LOXONE_TO_HA, default=True): bool,
    }
)


async def _test_connection(
    host: str, port: int, username: str, password: str,
    use_tls: bool = True, verify_ssl: bool = False,
) -> tuple[bool, str]:
    """Test the connection to the Loxone Miniserver via HTTPS."""
    import ssl as ssl_mod
    import certifi

    scheme = "https" if use_tls else "http"
    url = f"{scheme}://{host}:{port}/jdev/cfg/api"

    # Determine SSL parameter
    if use_tls:
        if verify_ssl:
            ssl_param = ssl_mod.create_default_context(cafile=certifi.where())
        else:
            ssl_param = False  # TLS active, skip cert verification (self-signed)
    else:
        ssl_param = None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                auth=aiohttp.BasicAuth(username, password),
                ssl=ssl_param,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    version = data.get("LL", {}).get("value", "unknown")
                    return True, version
                if resp.status == 401:
                    return False, "invalid_auth"
                return False, f"http_{resp.status}"
    except aiohttp.ClientConnectorError:
        return False, "cannot_connect"
    except aiohttp.ClientConnectorCertificateError:
        return False, "ssl_error"
    except TimeoutError:
        return False, "timeout"
    except Exception as err:
        _LOGGER.error("Unexpected error testing connection: %s", type(err).__name__)
        return False, "unknown"


class LoxoneBridgeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Loxone Bridge."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            use_tls = user_input.get(CONF_USE_TLS, DEFAULT_USE_TLS)
            verify_ssl = user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)

            # Prevent duplicate entries for the same host
            await self.async_set_unique_id(f"loxone_{host}_{port}")
            self._abort_if_unique_id_configured()

            success, result = await _test_connection(
                host, port, username, password, use_tls, verify_ssl
            )

            if success:
                return self.async_create_entry(
                    title=f"Loxone ({host})",
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                        CONF_USE_TLS: use_tls,
                        CONF_VERIFY_SSL: verify_ssl,
                        CONF_SYNC_HA_TO_LOXONE: user_input.get(CONF_SYNC_HA_TO_LOXONE, True),
                        CONF_SYNC_LOXONE_TO_HA: user_input.get(CONF_SYNC_LOXONE_TO_HA, True),
                    },
                )

            if result == "invalid_auth":
                errors["base"] = "invalid_auth"
            elif result == "cannot_connect":
                errors["base"] = "cannot_connect"
            elif result == "ssl_error":
                errors["base"] = "ssl_error"
            elif result == "timeout":
                errors["base"] = "timeout"
            else:
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> LoxoneBridgeOptionsFlow:
        """Return the options flow handler."""
        return LoxoneBridgeOptionsFlow(config_entry)


class LoxoneBridgeOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Loxone Bridge."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SYNC_HA_TO_LOXONE,
                        default=self._config_entry.data.get(CONF_SYNC_HA_TO_LOXONE, True),
                    ): bool,
                    vol.Required(
                        CONF_SYNC_LOXONE_TO_HA,
                        default=self._config_entry.data.get(CONF_SYNC_LOXONE_TO_HA, True),
                    ): bool,
                }
            ),
        )
