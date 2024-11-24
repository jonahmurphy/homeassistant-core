"""Support for the EPH Controls Ember themostats."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any
from enum import Enum
import base64
import datetime
import json
import time
import collections


from pyephember.pyephember import (
    EphEmber,
    ZoneMode,
    zone_current_temperature,
    #zone_is_active,
    zone_is_boost_active,
    zone_is_hot_water,
    zone_mode,
    zone_name,
    zone_target_temperature,
)
import voluptuous as vol

from homeassistant.components.climate import (
    PLATFORM_SCHEMA as CLIMATE_PLATFORM_SCHEMA,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_PASSWORD,
    CONF_USERNAME,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

_LOGGER = logging.getLogger(__name__)

# Return cached results if last scan was less then this time ago
SCAN_INTERVAL = timedelta(seconds=120)

OPERATION_LIST = [HVACMode.HEAT_COOL, HVACMode.HEAT, HVACMode.OFF]

PLATFORM_SCHEMA = CLIMATE_PLATFORM_SCHEMA.extend(
    {vol.Required(CONF_USERNAME): cv.string, vol.Required(CONF_PASSWORD): cv.string}
)

EPH_TO_HA_STATE = {
    "AUTO": HVACMode.HEAT_COOL,
    "ON": HVACMode.HEAT,
    "OFF": HVACMode.OFF,
}

HA_STATE_TO_EPH = {value: key for key, value in EPH_TO_HA_STATE.items()}


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the ephember thermostat."""
    username = config.get(CONF_USERNAME)
    password = config.get(CONF_PASSWORD)

    try:
        ember = EphEmber(username, password)
        zones = ember.get_zones()
        for zone in zones:
            add_entities([EphEmberThermostat(ember, zone)])
    except RuntimeError:
        _LOGGER.error("Cannot connect to EphEmber")
        return

    return

# New changes
class ZoneMode(Enum):
    """
    Modes that a zone can be set too
    """
    # pylint: disable=invalid-name
    AUTO = 0
    ALL_DAY = 1
    ON = 2
    OFF = 3
    
def zone_is_active(zone):
    """
    Check if the zone is on.
    This is a bit of a hack as the new API doesn't have a currently
    active variable
    """
    _LOGGER.error("GOT zone: %s", zone)
    # not sure how accurate the following tests are
    if (zone_is_scheduled_on(zone) or zone_advance_active(zone)) and not zone_is_target_temperature_reached(zone):
        return True
    if zone_boost_hours(zone) > 0 and not zone_is_target_boost_temperature_reached(zone):
        return True

    return False
  
  
def zone_is_target_temperature_reached(zone):
    return zone_current_temperature(zone) >= zone_target_temperature(zone)

def zone_is_target_boost_temperature_reached(zone):
    return zone_boost_temperature(zone) >= zone_target_temperature(zone)
    

def zone_is_scheduled_on(zone):
    """
    Check if zone is scheduled to be on
    """
    mode = zone_mode(zone)
    if mode == ZoneMode.OFF:
        return False

    if mode == ZoneMode.ON:
        return True

    def scheduletime_to_time(stime):
        """
        Convert from string time in format 12:30
        to python datetime
        """
        time_str = '13::55::26'
        return datetime.datetime.strptime(stime, '%H:%M').time()
    
    zone_tstamp = None
    zone_ts_time = None
    try:
        zone_tstamp = time.gmtime(zone['timestamp']/1000)
        zone_ts_time = datetime.time(zone_tstamp.tm_hour, zone_tstamp.tm_min)
    except:
        _LOGGER.error("Error getting timestamp from zone, using current times")
        zone_tstamp = time.gmtime()
        zone_ts_time = datetime.time(zone_tstamp.tm_hour, zone_tstamp.tm_min)
  
    for day in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']:
        program = zone['programmes'][day]
        if mode == ZoneMode.AUTO:
            for period in ['p1', 'p2', 'p3']:
                start_time = scheduletime_to_time(program[period]['starttime'])
                end_time = scheduletime_to_time(program[period]['endtime'])
                if start_time <= zone_ts_time <= end_time:
                    return True
        elif mode == ZoneMode.ALL_DAY:
            start_time = scheduletime_to_time(program['p1']['starttime'])
            end_time = scheduletime_to_time(program['p3']['endtime'])
            if start_time <= zone_ts_time <= end_time:
                return True
    return False
    
def zone_boost_temperature(zone):
    """
    Get target temperature for this zone
    """
    boost_activation = _zone_boostactivation(zone)
    if boost_activation is None:
        return None
    return boost_activation.get('targettemperature', None)  
 
def zone_advance_active(zone):
    """
    Check if zone has advance active
    """
    return zone.get('isadvanceactive', False)


def zone_boost_hours(zone):
    """
    Return zone boost hours
    """
    if not zone_is_boost_active(zone):
        return 0
    boost_activations = _zone_boostactivation(zone)
    if not boost_activations:
        return 0
    return boost_activations.get('numberofhours', 0)

def zone_boost_timestamp(zone):
    """
    Return zone boost hours
    """
    if not zone_is_boost_active(zone):
        return None
    boost_activations = _zone_boostactivation(zone)
    if not boost_activations:
        return None
    return boost_activations.get('activatedon', None)
    
def _zone_boostactivation(zone):
    return zone.get('boostActivations', None)

    
class EphEmberThermostat(ClimateEntity):
    """Representation of a EphEmber thermostat."""

    _attr_hvac_modes = OPERATION_LIST
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, ember, zone):
        """Initialize the thermostat."""
        self._ember = ember
        self._zone_name = zone_name(zone)
        self._zone = zone
        self._hot_water = zone_is_hot_water(zone)

        self._attr_name = self._zone_name

        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.AUX_HEAT
        )
        self._attr_target_temperature_step = 0.5
        if self._hot_water:
            self._attr_supported_features = ClimateEntityFeature.AUX_HEAT
            self._attr_target_temperature_step = None
        self._attr_supported_features |= (
            ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
        )

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return zone_current_temperature(self._zone)

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        _LOGGER.error("Testing JONAH")
        return zone_target_temperature(self._zone)

    @property
    def hvac_action(self) -> HVACAction:
        """Return current HVAC action."""
        if zone_is_active(self._zone):
            return HVACAction.HEATING

        return HVACAction.IDLE

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current operation ie. heat, cool, idle."""
        mode = zone_mode(self._zone)
        return self.map_mode_eph_hass(mode)

    def set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the operation mode."""
        mode = self.map_mode_hass_eph(hvac_mode)
        if mode is not None:
            self._ember.set_mode_by_name(self._zone_name, mode)
        else:
            _LOGGER.error("Invalid operation mode provided %s", hvac_mode)

    @property
    def is_aux_heat(self):
        """Return true if aux heater."""

        return zone_is_boost_active(self._zone)

    def turn_aux_heat_on(self) -> None:
        """Turn auxiliary heater on."""
        self._ember.activate_boost_by_name(
            self._zone_name, zone_target_temperature(self._zone)
        )

    def turn_aux_heat_off(self) -> None:
        """Turn auxiliary heater off."""
        self._ember.deactivate_boost_by_name(self._zone_name)

    def set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return

        if self._hot_water:
            return

        if temperature == self.target_temperature:
            return

        if temperature > self.max_temp or temperature < self.min_temp:
            return

        self._ember.set_target_temperture_by_name(self._zone_name, temperature)

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        # Hot water temp doesn't support being changed
        if self._hot_water:
            return zone_target_temperature(self._zone)

        return 5.0

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        if self._hot_water:
            return zone_target_temperature(self._zone)

        return 35.0

    def update(self) -> None:
        """Get the latest data."""
        self._zone = self._ember.get_zone(self._zone_name)

    @staticmethod
    def map_mode_hass_eph(operation_mode):
        """Map from Home Assistant mode to eph mode."""
        return getattr(ZoneMode, HA_STATE_TO_EPH.get(operation_mode), None)

    @staticmethod
    def map_mode_eph_hass(operation_mode):
        """Map from eph mode to Home Assistant mode."""
        return EPH_TO_HA_STATE.get(operation_mode.name, HVACMode.HEAT_COOL)
