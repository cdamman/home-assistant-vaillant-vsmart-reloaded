"""The Vaillant vSMART climate platform."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    HVACAction,
    HVACMode,
    ClimateEntityFeature,
    PRESET_AWAY,
    PRESET_NONE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from vaillant_netatmo_api import ApiException, SetpointMode, SystemMode

from .const import (
    DOMAIN,
)
from .entity import VaillantCoordinator, VaillantModuleEntity

_LOGGER = logging.getLogger(__name__)

DEFAULT_TEMPERATURE_INCREASE = 1

# Netatmo error code returned when the thermostat refuses an operation, for
# instance when asked to cancel a setpoint which is not active. The API answers
# it with a 403, which the API client reports as an expired access token.
FORBIDDEN_OPERATION_ERROR = 13


def _netatmo_error_code(ex: ApiException) -> int | None:
    """Return the error code carried by an API error response, if any."""

    response = getattr(ex, "response", None)

    if not response:
        return None

    try:
        return json.loads(response["body"])["error"]["code"]
    except (KeyError, TypeError, ValueError):
        return None


def _api_error(action: str, ex: ApiException) -> HomeAssistantError:
    """Turn an API exception into an error which can be shown to the user."""

    if _netatmo_error_code(ex) == FORBIDDEN_OPERATION_ERROR:
        return HomeAssistantError(
            f"Vaillant refused to {action}. The thermostat does not accept this "
            "change in its current system mode."
        )

    return HomeAssistantError(f"Error while trying to {action}: {ex}")

SUPPORTED_FEATURES = (
    ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
)
SUPPORTED_HVAC_MODES = [HVACMode.AUTO, HVACMode.HEAT]
SUPPORTED_PRESET_MODES = [PRESET_NONE, PRESET_AWAY]

homes_get_data = []

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_devices: AddEntitiesCallback
):
    """Set up Vaillant vSMART from a config entry."""

    coordinator: VaillantCoordinator = hass.data[DOMAIN][entry.entry_id]

    new_devices = [
        VaillantClimate(coordinator, device.id, module.id)
        for device in coordinator.data.devices.values()
        for module in device.modules
    ]
    async_add_devices(new_devices)


class VaillantClimate(VaillantModuleEntity, ClimateEntity):
    """Vaillant vSMART Climate."""

    @property
    def name(self) -> str:
        """Return the name of the climate."""

        return None

    @property
    def supported_features(self) -> int:
        """Return the flag of supported features for the climate."""

        return SUPPORTED_FEATURES

    @property
    def temperature_unit(self) -> str:
        """Return the measurement unit for all temperature values."""

        return UnitOfTemperature.CELSIUS

    @property
    def current_temperature(self) -> float:
        """Return the current room temperature."""

        return self._module.measured.temperature

    @property
    def target_temperature(self) -> float:
        """Return the targeted room temperature."""

        return (
            self._module.measured.est_setpoint_temp
            if self._module.measured.est_setpoint_temp is not None
            else self._module.measured.setpoint_temp
        )

    @property
    def hvac_action(self) -> HVACAction:
        """Return the currently running HVAC action."""

        if self._device.system_mode in [SystemMode.FROSTGUARD, SystemMode.SUMMER]:
            return HVACAction.OFF

        if self._module.boiler_status is True:
            return HVACAction.HEATING

        return HVACAction.IDLE

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available HVAC operation modes."""

        return SUPPORTED_HVAC_MODES

    @property
    def hvac_mode(self) -> HVACMode:
        """Return currently selected HVAC operation mode."""

        if self._module.setpoint_manual.setpoint_activate:
            return HVACMode.HEAT

        return HVACMode.AUTO

    @property
    def preset_modes(self) -> list[str]:
        """Return the list of available HVAC preset modes."""

        return SUPPORTED_PRESET_MODES

    @property
    def preset_mode(self) -> str:
        """Return the currently selected HVAC preset mode."""

        if self._module.setpoint_away.setpoint_activate:
            return PRESET_AWAY

        return PRESET_NONE

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Select new HVAC operation mode."""

        _LOGGER.debug("Setting HVAC mode to: %s", hvac_mode)

        if hvac_mode == HVACMode.HEAT:
            endtime = datetime.now() + timedelta(
                minutes=self._device.setpoint_default_duration
            )
            new_temperature = (
                self._module.measured.temperature + DEFAULT_TEMPERATURE_INCREASE
            )
            try:
                await self._client.async_set_minor_mode(
                    self._device_id,
                    self._module_id,
                    SetpointMode.MANUAL,
                    True,
                    setpoint_endtime=endtime,
                    setpoint_temp=new_temperature,
                )
            except ApiException as ex:
                raise _api_error("switch to heat mode", ex) from ex
        elif hvac_mode == HVACMode.AUTO:
            try:
                await self._client.async_set_minor_mode(
                    self._device_id,
                    self._module_id,
                    SetpointMode.MANUAL,
                    False,
                )
            except ApiException as ex:
                # Cancelling a manual setpoint which is not active is refused
                # by the API, and the thermostat is then already in the
                # requested state. The state of the setpoint cannot be checked
                # up front: a setpoint placed through the room API does not
                # show up in the module data this integration polls.
                if _netatmo_error_code(ex) != FORBIDDEN_OPERATION_ERROR:
                    raise _api_error("switch to auto mode", ex) from ex

                _LOGGER.debug("No manual setpoint to cancel: %s", ex)

        await self.coordinator.async_request_refresh()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Select new HVAC preset mode."""

        _LOGGER.debug("Setting HVAC preset mode to: %s", preset_mode)

        if preset_mode == PRESET_AWAY:
            try:
                await self._client.async_set_minor_mode(
                    self._device_id,
                    self._module_id,
                    SetpointMode.AWAY,
                    True,
                )
            except ApiException as ex:
                raise _api_error("switch to away mode", ex) from ex
        elif preset_mode == PRESET_NONE:
            try:
                await self._client.async_set_minor_mode(
                    self._device_id,
                    self._module_id,
                    SetpointMode.AWAY,
                    False,
                )
            except ApiException as ex:
                # Same as for the manual setpoint: cancelling an away setpoint
                # which is not active is refused, and already the wanted state.
                if _netatmo_error_code(ex) != FORBIDDEN_OPERATION_ERROR:
                    raise _api_error("cancel away mode", ex) from ex

                _LOGGER.debug("No away setpoint to cancel: %s", ex)

        await self.coordinator.async_request_refresh()

    async def async_set_temperature(self, **kwargs) -> None:
        """Update target room temperature value."""
        global homes_get_data

        new_temperature = kwargs.get(ATTR_TEMPERATURE)
        if new_temperature is None:
            return
            
        _LOGGER.debug("set_temperature called with arguments device_id %s module_id %s",self._device_id,self._module_id)

        if len(homes_get_data)==0:
            try:
                _LOGGER.debug("set_temperature calling get_home_data") 
                homes_get_data = await self._client.async_get_home_data() 
            except ApiException as ex:
                raise _api_error("fetch the home data", ex) from ex

        if len(homes_get_data)==1:
            _HOME_ID = homes_get_data[0].home_id
            _ROOM_ID = homes_get_data[0].rooms[0].room_id 
        else:
            for home in homes_get_data:
                if self._device_id == home.rooms[0].room_device_id:
                    _HOME_ID = home.home_id 
                    _ROOM_ID = home.rooms[0].room_id
                    break

        _LOGGER.debug("Setting target temperature to: %s", new_temperature)

        endtime = datetime.now() + timedelta(
            minutes=self._device.setpoint_default_duration
        )

        _LOGGER.debug("calling set_state_room home_id %s room_id %s",_HOME_ID,_ROOM_ID)

        try:
            await self._client.async_set_state_room(
                _HOME_ID,
                _ROOM_ID,
                SetpointMode.MANUAL,
                True,
                setpoint_endtime=endtime,
                setpoint_temp=new_temperature,
            )
        except ApiException as ex:
            raise _api_error("set the target temperature", ex) from ex

        await self.coordinator.async_request_refresh()
