"""Support for Netatmo energy devices (relays, thermostats and valves)."""
from __future__ import annotations

import logging
from abc import ABC
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum

from .auth import AbstractAsyncAuth, NetatmoOAuth2
from .exceptions import InvalidHome, NoSchedule
from .helpers import extract_raw_data_new
from .thermostat import (
    _GETHOMESDATA_REQ,
    _GETHOMESTATUS_REQ,
    _SETTHERMMODE_REQ,
    _SWITCHHOMESCHEDULE_REQ,
)

LOG = logging.getLogger(__name__)

# pylint: disable=W0613,R0201


class NetatmoDeviceType(Enum):
    """Class to represent Netatmo device types."""

    # temporarily disable locally-disabled and locally-enabled
    # pylint: disable=I0011,C0103

    # Climate/Energy
    NRV = "NRV"  # Smart valve
    NATherm1 = "NATherm1"  # Smart thermostat
    OTM = "OTM"  # OpenTherm modulating thermostat
    NAPlug = "NAPlug"  # Relay
    OTH = "OTH"  # OpenTherm relay

    # Cameras/Security
    NOC = "NOC"  # Smart Outdoor Camera (with Siren)
    NACamera = "NACamera"  # Smart Indoor Camera
    NSD = "NSD"  # Smart Smoke Detector
    NIS = "NIS"  # Smart Indoor Siren
    NACamDoorTag = "NACamDoorTag"  # Smart Door and Window Sensors

    # Weather
    NAMain = "NAMain"  # Smart Home Weather Station
    NAModule1 = "NAModule1"
    NAModule2 = "NAModule2"
    NAModule3 = "NAModule3"
    NAModule4 = "NAModule4"

    # Home Coach
    NHC = "NHC"  # Smart Indoor Air Quality Monitor

    # pylint: enable=I0011,C0103


@dataclass
class NetatmoHome:
    """Class to represent a Netatmo home."""

    entity_id: str
    name: str
    rooms: dict[str, NetatmoRoom]
    modules: dict[str, NetatmoModule]
    schedules: dict[str, NetatmoSchedule]

    def __init__(self, raw_data: dict) -> None:
        self.entity_id = raw_data["id"]
        self.name = raw_data.get("name", "Unknown")
        self.modules = {
            module["id"]: NetatmoModule(home=self, module=module)
            for module in raw_data.get("modules", [])
        }
        self.rooms = {
            room["id"]: NetatmoRoom(
                home=self,
                room=room,
                all_modules=self.modules,
            )
            for room in raw_data.get("rooms", [])
        }
        self.schedules = {
            s["id"]: NetatmoSchedule(home_id=self.entity_id, raw_data=s)
            for s in raw_data.get("schedules", [])
        }

    def update(self, raw_data: dict) -> None:
        for module in raw_data["errors"]:
            self.modules[module["id"]].update({})

        data = raw_data["home"]

        for module in data.get("modules", []):
            self.modules[module["id"]].update(module)

        for room in data.get("rooms", []):
            self.rooms[room["id"]].update(room)

    def get_selected_schedule(self) -> NetatmoSchedule | None:
        """Return selected schedule for given home."""
        for schedule in self.schedules.values():
            if schedule.selected:
                return schedule
        return None

    def is_valid_schedule(self, schedule_id: str) -> bool:
        """Check if valid schedule."""
        return schedule_id in self.schedules

    def get_hg_temp(self) -> float | None:
        """Return frost guard temperature value for given home."""
        if (schedule := self.get_selected_schedule()) is None:
            return None
        return schedule.hg_temp

    def get_away_temp(self) -> float | None:
        """Return configured away temperature value for given home."""
        if (schedule := self.get_selected_schedule()) is None:
            return None
        return schedule.away_temp


@dataclass
class NetatmoRoom:
    """Class to represent a Netatmo room."""

    entity_id: str
    name: str
    home: NetatmoHome
    modules: dict[str, NetatmoModule]

    reachable: bool = False
    therm_setpoint_temperature: float | None = None
    therm_setpoint_mode: str | None = None
    therm_measured_temperature: float | None = None
    heating_power_request: int | None = None

    def __init__(
        self,
        home: NetatmoHome,
        room: dict,
        all_modules: dict[str, NetatmoModule],
    ) -> None:
        self.entity_id = room["id"]
        self.name = room["name"]
        self.home = home
        self.modules = {
            m_id: m
            for m_id, m in all_modules.items()
            if m_id in room.get("module_ids", [])
        }

    def update(self, raw_data: dict) -> None:
        self.reachable = raw_data.get("reachable", False)
        self.therm_measured_temperature = raw_data.get("therm_measured_temperature")
        self.therm_setpoint_mode = raw_data.get("therm_setpoint_mode")
        self.therm_setpoint_temperature = raw_data.get("therm_setpoint_temperature")

    async def async_set_room_thermpoint(
        self,
        mode: str,
        temp: float = None,
        end_time: int = None,
    ) -> str | None:
        """Set room themperature set point."""
        ...


@dataclass
class NetatmoSchedule:
    """Class to represent a Netatmo room."""

    entity_id: str
    name: str
    home_id: str
    selected: bool
    away_temp: float | None
    hg_temp: float | None

    def __init__(self, home_id: str, raw_data) -> None:
        self.entity_id = raw_data["id"]
        self.name = raw_data["name"]
        self.home_id = home_id
        self.selected = raw_data.get("selected", False)
        self.hg_temp = raw_data.get("hg_temp")
        self.away_temp = raw_data.get("away_temp")


@dataclass
class NetatmoModule:
    """Class to represent a Netatmo module."""

    entity_id: str
    name: str
    device_type: Enum
    home: NetatmoHome
    room_id: str | None

    reachable: bool
    bridge: NetatmoModule | None
    modules: list[str]

    battery_state: str | None = None
    battery_level: int | None = None
    boiler_status: bool | None = None

    def __init__(self, home: NetatmoHome, module: dict) -> None:
        self.entity_id = module["id"]
        self.name = module["name"]
        self.device_type = module["type"]
        self.home = home
        self.room_id = module.get("room_id")
        self.reachable = False
        self.bridge = module.get("bridge")
        self.modules = module.get("modules_bridged", [])

    def update(self, raw_data: dict) -> None:
        self.reachable = raw_data.get("reachable", False)
        self.boiler_status = raw_data.get("boiler_status")
        self.battery_level = raw_data.get("battery_level")
        self.battery_state = raw_data.get("battery_state")

        if not self.reachable:
            # Update bridged modules and associated rooms
            for module_id in self.modules:
                module = self.home.modules[module_id]
                module.update(raw_data)
                if module.room_id:
                    self.home.rooms[module.room_id].update(raw_data)


class AbstractClimate(ABC):
    """Abstract class of Netatmo energy devices."""

    raw_data: dict = defaultdict(dict)
    homes: dict = defaultdict(dict)
    modules: dict = defaultdict(dict)
    rooms: dict = defaultdict(dict)
    thermostats: dict = defaultdict(dict)
    valves: dict = defaultdict(dict)
    relays: dict = defaultdict(dict)
    errors: dict = defaultdict(dict)
    schedules: dict = defaultdict(dict)
    zones: dict = defaultdict(dict)
    setpoint_duration: dict = defaultdict(dict)

    topology_timestamp: int | None

    def process(self, raw_data: dict) -> None:
        """Process raw data from the energy endpoint."""
        if "home" in raw_data:
            # Process status information from /homestatus
            self.homes[raw_data["home"]["id"]].update(raw_data)

        elif "homes" in raw_data:
            # Process topology information from /homedata
            self.homes = {
                item["id"]: NetatmoHome(raw_data=item) for item in raw_data["homes"]
            }


class AsyncClimate(AbstractClimate):
    """Class of Netatmo energy devices."""

    def __init__(self, auth: AbstractAsyncAuth) -> None:
        """Initialize the Netatmo home data.

        Arguments:
            auth {AbstractAsyncAuth} -- Authentication information with a valid access token
        """
        self.auth = auth

    async def async_update(self):
        """Fetch and process data from API."""
        if not self.homes:
            await self.async_update_topology()

        resp = await self.auth.async_post_request(url=_GETHOMESTATUS_REQ)
        raw_data = extract_raw_data_new(await resp.json(), "home")
        self.process(raw_data)

    async def async_update_topology(self) -> None:
        """Retrieve status updates from /homesdata."""
        resp = await self.auth.async_post_request(url=_GETHOMESDATA_REQ)
        raw_data = extract_raw_data_new(await resp.json(), "homes")
        self.process(raw_data)

    async def async_set_thermmode(
        self,
        home_id: str,
        mode: str,
        end_time: int = None,
        schedule_id: str = None,
    ) -> str | None:
        """Set thermotat mode."""
        if home_id not in self.homes:
            raise InvalidHome(f"{home_id} is not a valid home id.")

        if schedule_id is not None and not self.homes[home_id].is_valid_schedule(
            schedule_id,
        ):
            raise NoSchedule(f"{schedule_id} is not a valid schedule id.")

        if mode is None:
            raise NoSchedule(f"{mode} is not a valid mode.")

        post_params = {"home_id": home_id, "mode": mode}
        if end_time is not None and mode in {"hg", "away"}:
            post_params["endtime"] = str(end_time)

        if schedule_id is not None and mode == "schedule":
            post_params["schedule_id"] = schedule_id

        resp = await self.auth.async_post_request(
            url=_SETTHERMMODE_REQ,
            params=post_params,
        )
        assert not isinstance(resp, bytes)
        return await resp.json()

    async def async_switch_home_schedule(self, home_id: str, schedule_id: str) -> None:
        """Switch the schedule for a give home ID."""
        if not self.homes[home_id].is_valid_schedule(schedule_id):
            raise NoSchedule(f"{schedule_id} is not a valid schedule id")

        resp = await self.auth.async_post_request(
            url=_SWITCHHOMESCHEDULE_REQ,
            params={"home_id": home_id, "schedule_id": schedule_id},
        )
        LOG.debug("Response: %s", resp)


class Climate(AbstractClimate):
    """Class of Netatmo energy devices."""

    def __init__(self, auth: NetatmoOAuth2) -> None:
        """Initialize the Netatmo home data.

        Arguments:
            auth {NetatmoOAuth2} -- Authentication information with a valid access token
        """
        self.auth = auth

    def update(self):
        """Fetch and process data from API."""
        if not self.homes:
            self.update_topology()

        resp = self.auth.post_request(url=_GETHOMESTATUS_REQ)

        raw_data = extract_raw_data_new(resp.json(), "home")
        self.process(raw_data)

    def update_topology(self) -> None:
        """Retrieve status updates from /homesdata."""
        resp = self.auth.post_request(url=_GETHOMESDATA_REQ)

        raw_data = extract_raw_data_new(resp.json(), "homes")
        self.process(raw_data)
