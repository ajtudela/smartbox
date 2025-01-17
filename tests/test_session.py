import datetime
from freezegun import freeze_time
import pytest
import smartbox

_MOCK_API_NAME = 'myapi'
_MOCK_BASIC_AUTH_CREDS = 'sldjfls93r2lkj'
_MOCK_USERNAME = 'xxxxx'
_MOCK_PASSWORD = 'yyyyy'
_MOCK_TOKEN_TYPE = 'bearer'
_MOCK_ACCESS_TOKEN = 'sj32oj2lkwjf'
_MOCK_REFRESH_TOKEN = '23ij2oij324j3423'
_MOCK_EXPIRES_IN = 14400
_MOCK_DEV_ID = '2o3jo2jkj'
_MOCK_DEV_NAME = 'My device'


@pytest.fixture
def session(requests_mock):
    requests_mock.post(f"https://api-{_MOCK_API_NAME}.helki.com/client/token",
                       json={
                           'token_type': _MOCK_TOKEN_TYPE,
                           'access_token': _MOCK_ACCESS_TOKEN,
                           'expires_in': _MOCK_EXPIRES_IN,
                           'refresh_token': _MOCK_REFRESH_TOKEN,
                       })
    return smartbox.Session(_MOCK_API_NAME, _MOCK_BASIC_AUTH_CREDS, _MOCK_USERNAME, _MOCK_PASSWORD)


def test_auth(requests_mock, session):
    '''Test initial token request'''
    assert requests_mock.last_request.text == f'grant_type=password&username={_MOCK_USERNAME}&password={_MOCK_PASSWORD}'
    assert requests_mock.last_request.headers['authorization'] == f"Basic {_MOCK_BASIC_AUTH_CREDS}"
    assert session.get_api_name() == _MOCK_API_NAME


def test_get_devices(requests_mock, session):
    requests_mock.get(f"https://api-{_MOCK_API_NAME}.helki.com/api/v2/devs",
                      json={'devs': [{
                          'dev_id': _MOCK_DEV_ID,
                          'name': _MOCK_DEV_NAME,
                      }]})
    resp = session.get_devices()
    assert len(resp) == 1
    assert resp[0]['dev_id'] == _MOCK_DEV_ID
    assert resp[0]['name'] == _MOCK_DEV_NAME


def test_get_nodes(requests_mock, session):
    node_1 = {'addr': 1, 'name': 'My heater', 'type': 'htr'}
    node_2 = {'addr': 2, 'name': 'My other heater', 'type': 'htr'}
    requests_mock.get(f"https://api-{_MOCK_API_NAME}.helki.com/api/v2/devs/{_MOCK_DEV_ID}/mgr/nodes",
                      json={'nodes': [node_1, node_2]})
    resp = session.get_nodes(_MOCK_DEV_ID)
    assert len(resp) == 2
    assert resp[0] == node_1
    assert resp[1] == node_2


def test_status(requests_mock, session):
    node_1 = {'addr': 1, 'name': 'My heater', 'type': 'htr'}

    requests_mock.get(
        f"https://api-{_MOCK_API_NAME}.helki.com/api/v2/devs/{_MOCK_DEV_ID}/{node_1['type']}/{node_1['addr']}/status",
        json={
            'mode': 'auto',
            'stemp': '16.0',
            'mtemp': '19.2'
        })
    resp = session.get_status(_MOCK_DEV_ID, node_1)
    assert resp['mode'] == 'auto'
    assert resp['stemp'] == '16.0'
    assert resp['mtemp'] == '19.2'

    with pytest.raises(ValueError):
        resp = session.set_status(_MOCK_DEV_ID, node_1, {'stemp': '17.0'})

    requests_mock.post(
        f"https://api-{_MOCK_API_NAME}.helki.com/api/v2/devs/{_MOCK_DEV_ID}/{node_1['type']}/{node_1['addr']}/status",
        json={
            'mode': 'auto',
            'stemp': '17.0',
            'mtemp': '19.2'
        })
    resp = session.set_status(_MOCK_DEV_ID, node_1, {'stemp': '17.0', 'units': 'C'})
    assert requests_mock.last_request.json() == {'stemp': '17.0', 'units': 'C'}


def test_setup(requests_mock, session):
    node_2 = {'addr': 2, 'name': 'My other heater', 'type': 'htr'}

    requests_mock.get(
        f"https://api-{_MOCK_API_NAME}.helki.com/api/v2/devs/{_MOCK_DEV_ID}/{node_2['type']}/{node_2['addr']}/setup",
        json={
            'away_mode': 0,
            'units': 'C'
        })
    resp = session.get_setup(_MOCK_DEV_ID, node_2)
    assert resp['away_mode'] == 0
    assert resp['units'] == 'C'

    requests_mock.post(
        f"https://api-{_MOCK_API_NAME}.helki.com/api/v2/devs/{_MOCK_DEV_ID}/{node_2['type']}/{node_2['addr']}/setup",
        json={
            'away_mode': 0,
            'units': 'F'
        })
    resp = session.set_setup(_MOCK_DEV_ID, node_2, {'units': 'F'})
    assert requests_mock.last_request.json() == {'away_mode': 0, 'units': 'F'}


def test_device_away_status(requests_mock, session):
    # away_status
    requests_mock.get(f"https://api-{_MOCK_API_NAME}.helki.com/api/v2/devs/{_MOCK_DEV_ID}/mgr/away_status",
                      json={
                          'away': False,
                          'enabled': True
                      })
    resp = session.get_device_away_status(_MOCK_DEV_ID)
    assert not resp['away']
    assert resp['enabled']

    requests_mock.post(f"https://api-{_MOCK_API_NAME}.helki.com/api/v2/devs/{_MOCK_DEV_ID}/mgr/away_status",
                       json={
                           'away': True,
                           'enabled': True
                       })
    resp = session.set_device_away_status(_MOCK_DEV_ID, {'away': True})
    assert requests_mock.last_request.json() == {'away': True}
    assert resp['away']
    assert resp['enabled']


def test_refresh(requests_mock):
    def token_request_matcher(request, grant_type):
        return f"grant_type={grant_type}" in request.text.split('&')

    # login response
    requests_mock.post(f"https://api-{_MOCK_API_NAME}.helki.com/client/token",
                       json={
                           'token_type': _MOCK_TOKEN_TYPE,
                           'access_token': _MOCK_ACCESS_TOKEN,
                           'expires_in': _MOCK_EXPIRES_IN,
                           'refresh_token': _MOCK_REFRESH_TOKEN,
                       },
                       additional_matcher=lambda request: token_request_matcher(request, "password"))

    # refresh response
    new_access_token = 'sf8s9f09dfsj'
    new_refresh_token = 'oij09j43rj434f'
    requests_mock.post(f"https://api-{_MOCK_API_NAME}.helki.com/client/token",
                       json={
                           'token_type': _MOCK_TOKEN_TYPE,
                           'access_token': new_access_token,
                           'expires_in': _MOCK_EXPIRES_IN,
                           'refresh_token': new_refresh_token,
                       },
                       additional_matcher=lambda request: token_request_matcher(request, "refresh_token"))

    requests_mock.get(f"https://api-{_MOCK_API_NAME}.helki.com/api/v2/devs",
                      json={'devs': [{
                          'dev_id': _MOCK_DEV_ID,
                          'name': _MOCK_DEV_NAME,
                      }]})

    with freeze_time("2021-01-15 23:23:45") as frozen_datetime:
        session = smartbox.Session(_MOCK_API_NAME, _MOCK_BASIC_AUTH_CREDS, _MOCK_USERNAME, _MOCK_PASSWORD)
        assert session.get_expiry_time() == (frozen_datetime() + datetime.timedelta(seconds=_MOCK_EXPIRES_IN))
        assert token_request_matcher(requests_mock.last_request, "password")
        assert session.get_access_token() == _MOCK_ACCESS_TOKEN
        assert session.get_refresh_token() == _MOCK_REFRESH_TOKEN

        # initial API call, no refresh needed
        session.get_devices()

        # move to 60s before expiry, no refresh should occur
        frozen_datetime.move_to(session.get_expiry_time() - datetime.timedelta(seconds=60))
        session.get_devices()

        # move to 5s before expiry, refresh should occur
        frozen_datetime.move_to(session.get_expiry_time() - datetime.timedelta(seconds=5))
        session.get_devices()
        assert token_request_matcher(requests_mock.request_history[-2], "refresh_token")
        assert session.get_expiry_time() == (frozen_datetime() + datetime.timedelta(seconds=_MOCK_EXPIRES_IN))
        assert session.get_access_token() == new_access_token
        assert session.get_refresh_token() == new_refresh_token

        # no refresh on next request
        requests_mock.reset_mock()
        session.get_devices()
        assert requests_mock.call_count == 1 and requests_mock.last_request.method == 'GET'
