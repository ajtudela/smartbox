import pytest
import json
from tests.common import load_fixture
from pydantic import ValidationError
from smartbox.models import (
    NodeFactoryOptions,
    NodeExtraOptions,
    NodeSetup,
    NodeStatus,
    Node,
    Nodes,
    Device,
    Devices,
    Home,
    Homes,
)


def test_node_factory_options():
    data = {
        "temp_compensation_enabled": True,
        "window_mode_available": True,
        "true_radiant_available": True,
        "duty_limit": 10,
        "boost_config": 1,
        "button_double_press": True,
        "prog_resolution": 5,
        "bbc_value": 2,
        "bbc_available": True,
        "lst_value": 3,
        "lst_available": True,
        "fil_pilote_available": True,
        "backlight_time": 30,
        "button_down_code": 1,
        "button_up_code": 2,
        "button_mode_code": 3,
        "button_prog_code": 4,
        "button_off_code": 5,
        "button_boost_code": 6,
        "splash_screen_type": 1,
    }
    options = NodeFactoryOptions(**data)
    assert options.temp_compensation_enabled == True
    assert options.duty_limit == 10


def test_node_extra_options():
    data = {"boost_temp": "22.5", "boost_time": 60}
    options = NodeExtraOptions(**data)
    assert options.boost_temp == "22.5"
    assert options.boost_time == 60


def test_node_setup():
    data = {
        "sync_status": "synced",
        "control_mode": 1,
        "units": "C",
        "power": "on",
        "offset": "0.5",
        "away_mode": 0,
        "away_offset": "1.0",
        "modified_auto_span": 10,
        "window_mode_enabled": True,
        "true_radiant_enabled": True,
        "user_duty_factor": 5,
        "flash_version": "1.0.0",
        "factory_options": {
            "temp_compensation_enabled": True,
            "window_mode_available": True,
            "true_radiant_available": True,
            "duty_limit": 10,
            "boost_config": 1,
            "button_double_press": True,
            "prog_resolution": 5,
            "bbc_value": 2,
            "bbc_available": True,
            "lst_value": 3,
            "lst_available": True,
            "fil_pilote_available": True,
            "backlight_time": 30,
            "button_down_code": 1,
            "button_up_code": 2,
            "button_mode_code": 3,
            "button_prog_code": 4,
            "button_off_code": 5,
            "button_boost_code": 6,
            "splash_screen_type": 1,
        },
        "extra_options": {"boost_temp": "22.5", "boost_time": 60},
    }
    setup = NodeSetup(**data)
    assert setup.sync_status == "synced"
    assert setup.control_mode == 1


def test_node_status():
    data = {
        "sync_status": "synced",
        "mode": "auto",
        "active": True,
        "ice_temp": "5.0",
        "eco_temp": "18.0",
        "comf_temp": "22.0",
        "units": "C",
        "stemp": "21.0",
        "mtemp": "20.0",
        "power": "on",
        "locked": 0,
        "duty": 50,
        "act_duty": 45,
        "pcb_temp": "30.0",
        "power_pcb_temp": "35.0",
        "presence": True,
        "window_open": False,
        "true_radiant_active": True,
        "boost": False,
        "boost_end_min": 0,
        "boost_end_day": 0,
        "error_code": "none",
        "on": True,
        "selected_temp": "eco",
        "comfort_temp": "24.3",
        "eco_offset": "4",
    }
    status = NodeStatus(**data)
    assert status.sync_status == "synced"
    assert status.mode == "auto"


def test_node():
    data = {
        "name": "Living Room",
        "addr": 1,
        "type": "thermostat",
        "installed": True,
        "lost": False,
        "status": {
            "sync_status": "synced",
            "mode": "auto",
            "active": True,
            "ice_temp": "5.0",
            "eco_temp": "18.0",
            "comf_temp": "22.0",
            "units": "C",
            "stemp": "21.0",
            "mtemp": "20.0",
            "power": "on",
            "locked": 0,
            "duty": 50,
            "act_duty": 45,
            "pcb_temp": "30.0",
            "power_pcb_temp": "35.0",
            "presence": True,
            "window_open": False,
            "true_radiant_active": True,
            "boost": False,
            "boost_end_min": 0,
            "boost_end_day": 0,
            "error_code": "none",
        },
        "setup": {
            "sync_status": "synced",
            "control_mode": 1,
            "units": "C",
            "power": "on",
            "offset": "0.5",
            "away_mode": 0,
            "away_offset": "1.0",
            "modified_auto_span": 10,
            "window_mode_enabled": True,
            "true_radiant_enabled": True,
            "user_duty_factor": 5,
            "flash_version": "1.0.0",
            "factory_options": {
                "temp_compensation_enabled": True,
                "window_mode_available": True,
                "true_radiant_available": True,
                "duty_limit": 10,
                "boost_config": 1,
                "button_double_press": True,
                "prog_resolution": 5,
                "bbc_value": 2,
                "bbc_available": True,
                "lst_value": 3,
                "lst_available": True,
                "fil_pilote_available": True,
                "backlight_time": 30,
                "button_down_code": 1,
                "button_up_code": 2,
                "button_mode_code": 3,
                "button_prog_code": 4,
                "button_off_code": 5,
                "button_boost_code": 6,
                "splash_screen_type": 1,
            },
            "extra_options": {"boost_temp": "22.5", "boost_time": 60},
        },
    }
    node = Node(**data)
    assert node.name == "Living Room"
    assert node.addr == 1


def test_nodes():
    data = {
        "nodes": [
            {
                "name": "Living Room",
                "addr": 1,
                "type": "thermostat",
                "installed": True,
                "lost": False,
            },
            {
                "name": "Bedroom",
                "addr": 2,
                "type": "thermostat",
                "installed": True,
                "lost": False,
            },
        ]
    }
    nodes = Nodes(**data)
    assert len(nodes.nodes) == 2
    assert nodes.nodes[0].name == "Living Room"
    assert nodes.nodes[1].name == "Bedroom"


def test_device():
    data = {
        "dev_id": "device1",
        "name": "Smart Thermostat",
        "product_id": "prod123",
        "fw_version": "1.0.0",
        "serial_id": "serial123",
        "nodes": [
            {
                "name": "Living Room",
                "addr": 1,
                "type": "thermostat",
                "installed": True,
                "lost": False,
            }
        ],
    }
    device = Device(**data)
    assert device.dev_id == "device1"
    assert device.name == "Smart Thermostat"


def test_devices():
    data = json.loads(load_fixture("devs.json"))
    devices = Devices(**data)
    assert len(devices.devs) == 2
    assert devices.devs[0].dev_id == "device1"
    assert devices.devs[1].dev_id == "device2"


def test_home():
    data = json.loads(load_fixture("grouped_devs.json"))[0]
    home = Home(**data)
    assert home.id == "home1"
    assert home.name == "My Home"


def test_homes():
    data = {"root": json.loads(load_fixture("grouped_devs.json"))}
    homes = Homes(**data)
    assert len(homes.root) == 2
    assert homes.root[0].id == "home1"
    assert homes.root[1].id == "home2"
