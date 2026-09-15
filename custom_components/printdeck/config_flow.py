"""Config flow for the PrintDeck integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import (
    PrintDeckApiClient,
    PrintDeckApiDisabledError,
    PrintDeckApiError,
    PrintDeckAuthenticationError,
    PrintDeckCannotConnectError,
    PrintDeckInfo,
    PrintDeckInvalidResponseError,
    PrintDeckUnsupportedError,
)
from .const import (
    CONF_TOKEN,
    CONF_TOPIC_ROOT,
    CONF_TRANSPORT,
    DEFAULT_HOST,
    DOMAIN,
    TRANSPORT_HTTP,
    TRANSPORT_MQTT,
)
from .mqtt_state import (
    PrintDeckDiscoveryConflict,
    decode_payload,
    parse_mqtt_info,
    validate_topic_root,
)
from .ownership import has_standard_mqtt_entities

_LOGGER = logging.getLogger(__name__)


def _normalize_host(value: str) -> str:
    host = value.strip()
    if "://" in host:
        parsed = urlsplit(host)
        if parsed.scheme != "http" or not parsed.netloc or parsed.path not in ("", "/"):
            raise ValueError("PrintDeck requires a local HTTP host without a path")
        host = parsed.netloc
    return host.rstrip("/").rstrip(".")


def _data_schema(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    values = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_HOST, default=values.get(CONF_HOST, DEFAULT_HOST)
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Required(CONF_TOKEN, default=values.get(CONF_TOKEN, "")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }
    )


def _token_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_TOKEN): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            )
        }
    )


class PrintDeckConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle setup, discovery, reauthentication and reconfiguration."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_host: str | None = None
        self._discovered_device_id: str | None = None
        self._reconfiguring = False

    def _abort_configured_http_host(self, device_id: str, host: str) -> None:
        """Keep HTTP address discovery working without altering MQTT entries."""
        existing = next(
            (
                entry
                for entry in self._async_current_entries()
                if entry.unique_id == device_id
            ),
            None,
        )
        updates = (
            {CONF_HOST: host}
            if existing is not None
            and existing.data.get(CONF_TRANSPORT, TRANSPORT_HTTP) == TRANSPORT_HTTP
            else None
        )
        self._abort_if_unique_id_configured(updates=updates)

    async def _async_validate(self, host: str, token: str) -> PrintDeckInfo:
        client = PrintDeckApiClient(async_get_clientsession(self.hass), host, token)
        return await client.async_get_info()

    async def _async_entry_from_input(
        self, user_input: dict[str, Any], *, reconfigure: bool = False
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        try:
            host = _normalize_host(user_input[CONF_HOST])
            token = user_input[CONF_TOKEN].strip()
            if not host or not token:
                raise ValueError
            info = await self._async_validate(host, token)
            if has_standard_mqtt_entities(self.hass, info.device_id):
                raise PrintDeckDiscoveryConflict("Standard MQTT entities still exist")
        except PrintDeckDiscoveryConflict:
            errors["base"] = "mqtt_discovery_conflict"
        except ValueError:
            errors["base"] = "invalid_input"
        except PrintDeckAuthenticationError:
            errors["base"] = "invalid_auth"
        except PrintDeckApiDisabledError:
            errors["base"] = "api_disabled"
        except PrintDeckCannotConnectError:
            errors["base"] = "cannot_connect"
        except PrintDeckUnsupportedError:
            errors["base"] = "unsupported_firmware"
        except PrintDeckInvalidResponseError:
            errors["base"] = "invalid_response"
        except PrintDeckApiError:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception while connecting to PrintDeck")
            errors["base"] = "unknown"
        else:
            await self.async_set_unique_id(info.device_id)
            data = {CONF_HOST: host, CONF_TOKEN: token, CONF_TRANSPORT: TRANSPORT_HTTP}
            if reconfigure:
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    self._get_reconfigure_entry(), data=data
                )
            self._abort_configured_http_host(info.device_id, host)
            return self.async_create_entry(title=info.name, data=data)

        return self.async_show_form(
            step_id="http",
            data_schema=_data_schema(user_input),
            errors=errors,
            description_placeholders={
                "configuration_url": f"http://{user_input.get(CONF_HOST, DEFAULT_HOST)}"
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose a transport without changing the PrintDeck entity identity."""
        if user_input is not None:
            return await self._async_entry_from_input(user_input)
        return self.async_show_menu(step_id="user", menu_options=["http", "mqtt"])

    async def async_step_http(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure the existing local HTTP API."""
        if user_input is not None:
            return await self._async_entry_from_input(
                user_input, reconfigure=self._reconfiguring
            )
        defaults = (
            dict(self._get_reconfigure_entry().data) if self._reconfiguring else {}
        )
        if self._discovered_host:
            defaults[CONF_HOST] = self._discovered_host
        return self.async_show_form(
            step_id="http",
            data_schema=_data_schema(defaults),
            description_placeholders={
                "configuration_url": f"http://{defaults.get(CONF_HOST, DEFAULT_HOST)}"
            },
        )

    async def _async_mqtt_info(self, root: str) -> PrintDeckInfo:
        """Read bounded retained identity using HA's own MQTT connection."""
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            raise PrintDeckCannotConnectError("MQTT is not configured")
        future = self.hass.loop.create_future()

        @callback
        def received(message: mqtt.ReceiveMessage) -> None:
            if future.done():
                return
            try:
                info = parse_mqtt_info(root, decode_payload(message.payload))
                if has_standard_mqtt_entities(self.hass, info.device_id):
                    raise PrintDeckDiscoveryConflict(
                        "Standard MQTT entities still exist"
                    )
            except (PrintDeckApiError, ValueError) as err:
                future.set_exception(err)
            else:
                future.set_result(info)

        unsubscribe = await mqtt.async_subscribe(
            self.hass, f"{root}/info", received, qos=1
        )
        try:
            async with asyncio.timeout(15):
                return await future
        finally:
            unsubscribe()

    def _known_mqtt_root(self) -> str | None:
        """Derive the topic for a discovered device or an existing entry."""
        entry = self._get_reconfigure_entry() if self._reconfiguring else None
        device_id = entry.unique_id if entry is not None else self._discovered_device_id
        root = entry.data.get(CONF_TOPIC_ROOT) if entry is not None else None
        if root is None and device_id:
            root = f"printdeck/{device_id}/v1"
        if not isinstance(root, str):
            return None
        try:
            return validate_topic_root(root)
        except ValueError:
            return None

    async def async_step_mqtt(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Connect automatically when discovery already identifies the device."""
        known_root = self._known_mqtt_root()
        if known_root is not None:
            user_input = {CONF_TOPIC_ROOT: known_root}
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                root = validate_topic_root(user_input[CONF_TOPIC_ROOT])
                info = await self._async_mqtt_info(root)
                if (
                    self._discovered_device_id is not None
                    and info.device_id != self._discovered_device_id
                ):
                    raise PrintDeckInvalidResponseError("MQTT discovery identity mismatch")
            except PrintDeckDiscoveryConflict:
                errors["base"] = "mqtt_discovery_conflict"
            except (ValueError, KeyError):
                errors["base"] = "invalid_topic_root"
            except TimeoutError:
                errors["base"] = "mqtt_no_device"
            except (PrintDeckCannotConnectError, HomeAssistantError):
                errors["base"] = "mqtt_not_ready"
            except PrintDeckApiError:
                errors["base"] = "invalid_response"
            else:
                await self.async_set_unique_id(info.device_id)
                data = {CONF_TRANSPORT: TRANSPORT_MQTT, CONF_TOPIC_ROOT: root}
                if self._reconfiguring:
                    self._abort_if_unique_id_mismatch()
                    return self.async_update_reload_and_abort(
                        self._get_reconfigure_entry(), data=data
                    )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info.name, data=data)
        if known_root is not None:
            return self.async_show_form(
                step_id="mqtt_auto",
                data_schema=vol.Schema({}),
                errors=errors,
            )
        defaults = dict(user_input or {})
        return self.async_show_form(
            step_id="mqtt",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TOPIC_ROOT, default=defaults.get(CONF_TOPIC_ROOT, "")
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
                }
            ),
            errors=errors,
        )

    async def async_step_mqtt_auto(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Retry automatic MQTT setup after the connection settings are corrected."""
        return await self.async_step_mqtt(user_input)

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a PrintDeck announced through mDNS."""
        device_id = discovery_info.properties.get("id")
        if not isinstance(device_id, str):
            return self.async_abort(reason="invalid_discovery")
        try:
            validate_topic_root(f"printdeck/{device_id}/v1")
        except ValueError:
            return self.async_abort(reason="invalid_discovery")
        await self.async_set_unique_id(device_id)
        self._abort_configured_http_host(device_id, discovery_info.host)
        self._discovered_device_id = device_id
        self._discovered_host = discovery_info.host.rstrip(".")
        suffix = device_id.removeprefix("printdeck-")[-6:].upper()
        self.context["title_placeholders"] = {
            "name": discovery_info.properties.get("name") or f"PrintDeck {suffix}"
        }
        self.context["configuration_url"] = f"http://{self._discovered_host}"
        return await self.async_step_user()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask only for the API token after automatic discovery."""
        assert self._discovered_host is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            complete_input = {
                CONF_HOST: self._discovered_host,
                CONF_TOKEN: user_input[CONF_TOKEN],
            }
            result = await self._async_entry_from_input(complete_input)
            if result["type"] != "form":
                return result
            errors = result.get("errors", {})
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=_token_schema(),
            errors=errors,
            description_placeholders={
                "name": self.context.get("title_placeholders", {}).get(
                    "name", "PrintDeck"
                ),
                "host": self._discovered_host,
                "configuration_url": f"http://{self._discovered_host}",
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after a rejected API token."""
        self.context["title_placeholders"] = {"name": self._get_reauth_entry().title}
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace a rejected API token."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            try:
                info = await self._async_validate(entry.data[CONF_HOST], token)
            except PrintDeckAuthenticationError:
                errors["base"] = "invalid_auth"
            except PrintDeckApiDisabledError:
                errors["base"] = "api_disabled"
            except PrintDeckUnsupportedError:
                errors["base"] = "unsupported_firmware"
            except PrintDeckApiError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(info.device_id)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: token}
                )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=_token_schema(), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change transport in place, preserving entities and automations."""
        self._reconfiguring = True
        return self.async_show_menu(
            step_id="reconfigure", menu_options=["http", "mqtt"]
        )
