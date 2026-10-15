from smartbox.models import (
    AcmNodeStatus,
    DefaultNodeStatus,
    Devices,
    Guests,
    GuestUser,
    HtrModNodeStatus,
    HtrNodeStatus,
    Node,
    NodeExtraOptions,
    NodeFactoryOptions,
    NodeSetup,
    NodeStatus,
    PmoSetup,
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
    assert options.temp_compensation_enabled
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


def test_node_setup_keeps_unknown_keys():
    """Unknown keys survive validation and model_dump (extra allow).

    Regression pin for the #1 follow-up (upstream 5618aa3): the setup
    endpoint requires the full payload to be re-posted, so a typed read
    must not lose fields our models do not declare.
    """
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
        "factory_options": {
            "temp_compensation_enabled": True,
            "window_mode_available": True,
            "true_radiant_available": True,
            "duty_limit": 10,
        },
        # Undeclared by both setup models (observed on pmo).
        "counter_offset": 42,
    }
    setup = NodeSetup(**data)
    assert setup.counter_offset == 42
    dumped = setup.model_dump(mode="json")
    assert dumped["counter_offset"] == 42

    pmo_setup = PmoSetup(
        circuit_type=1,
        power_limit=0,
        reverse=False,
        counter_offset=7,
    )
    assert pmo_setup.counter_offset == 7
    assert pmo_setup.model_dump(mode="json")["counter_offset"] == 7


def test_default_node_status():
    data = {
        "mtemp": "20.0",
        "units": "C",
        "sync_status": "synced",
        "locked": False,
        "mode": "auto",
        "error_code": "none",
        "eco_temp": "18.0",
        "comf_temp": "22.0",
        "act_duty": 45,
        "pcb_temp": "30.0",
        "power_pcb_temp": "35.0",
        "presence": True,
        "window_open": False,
        "true_radiant_active": True,
        "boost": False,
        "boost_end_min": 0,
        "boost_end_day": 0,
        "stemp": "21.0",
        "power": "on",
        "duty": 50,
        "ice_temp": "5.0",
        "active": True,
    }
    status = DefaultNodeStatus(**data)
    assert status.sync_status == "synced"
    assert status.mode == "auto"


def test_htr_mod_node_status():
    data = {
        "mtemp": "20.0",
        "units": "C",
        "sync_status": "synced",
        "locked": False,
        "mode": "auto",
        "error_code": "none",
        "eco_temp": "18.0",
        "comf_temp": "22.0",
        "act_duty": 45,
        "pcb_temp": "30.0",
        "power_pcb_temp": "35.0",
        "presence": True,
        "window_open": False,
        "true_radiant_active": True,
        "boost": False,
        "boost_end_min": 0,
        "boost_end_day": 0,
        "stemp": "21.0",
        "power": "on",
        "duty": 50,
        "ice_temp": "5.0",
        "active": True,
        "on": True,
        "selected_temp": "eco",
        "comfort_temp": "24.3",
        "eco_offset": "4",
    }
    status = HtrModNodeStatus(**data)
    assert status.sync_status == "synced"
    assert status.mode == "auto"
    assert status.on


def test_htr_node_status():
    data = {
        "mtemp": "20.0",
        "units": "C",
        "sync_status": "synced",
        "locked": False,
        "mode": "auto",
        "error_code": "none",
        "eco_temp": "18.0",
        "comf_temp": "22.0",
        "act_duty": 45,
        "pcb_temp": "30.0",
        "power_pcb_temp": "35.0",
        "presence": True,
        "window_open": False,
        "true_radiant_active": True,
        "boost": False,
        "boost_end_min": 0,
        "boost_end_day": 0,
        "stemp": "21.0",
        "power": "on",
        "duty": 50,
        "ice_temp": "5.0",
        "active": True,
    }
    status = HtrNodeStatus(**data)
    assert status.sync_status == "synced"
    assert status.mode == "auto"
    assert status.active


def test_acm_node_status():
    data = {
        "mtemp": "20.0",
        "units": "C",
        "sync_status": "synced",
        "locked": False,
        "mode": "auto",
        "error_code": "none",
        "eco_temp": "18.0",
        "comf_temp": "22.0",
        "act_duty": 45,
        "pcb_temp": "30.0",
        "power_pcb_temp": "35.0",
        "presence": True,
        "window_open": False,
        "true_radiant_active": True,
        "boost": False,
        "boost_end_min": 0,
        "boost_end_day": 0,
        "stemp": "21.0",
        "power": "on",
        "duty": 50,
        "ice_temp": "5.0",
        "active": True,
        "charging": True,
        "charge_level": 80,
    }
    status = AcmNodeStatus(**data)
    assert status.sync_status == "synced"
    assert status.mode == "auto"
    assert status.charging


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
    status = NodeStatus(root=HtrModNodeStatus(**data))
    assert status.root.sync_status == "synced"
    assert status.sync_status == "synced"
    assert status.mode == "auto"


def test_guest_user():
    data = {
        "pending": True,
        "email": "guest@example.com",
    }
    guest = GuestUser(**data)
    assert guest.pending
    assert guest.email == "guest@example.com"


def test_guests():
    data = {
        "guest_users": [
            {
                "pending": True,
                "email": "guest1@example.com",
            },
            {
                "pending": False,
                "email": "guest2@example.com",
            },
        ]
    }
    guests = Guests(**data)
    assert len(guests.guest_users) == 2
    assert guests.guest_users[0].email == "guest1@example.com"
    assert not guests.guest_users[1].pending


def test_devices_invited_to_default():
    """invited_to may be omitted by the server without breaking validation."""
    devices = Devices.model_validate({"devs": []})
    assert devices.devs == []
    assert devices.invited_to == []


def test_structural_models_keep_unknown_keys():
    """Unknown keys on node/device/home models survive a round-trip."""
    node = Node.model_validate(
        {
            "name": "n",
            "addr": 1,
            "type": "htr",
            "installed": True,
            "new_field": 7,
        }
    )
    assert node.model_dump()["new_field"] == 7
