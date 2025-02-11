from smartbox.models import (
    AcmNodeStatus,
    DefaultNodeStatus,
    Guests,
    GuestUser,
    HtrModNodeStatus,
    HtrNodeStatus,
    NodeExtraOptions,
    NodeFactoryOptions,
    NodeSetup,
    NodeStatus,
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
