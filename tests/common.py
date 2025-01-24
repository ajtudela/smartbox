import json
import pathlib


def get_fixture_path(filename: str) -> pathlib.Path:
    """Get path of fixture."""
    return pathlib.Path(__file__).parent.joinpath("fixtures", filename)


def load_fixture(filename):
    """Load a fixture."""
    return get_fixture_path(filename).read_text(encoding="utf-8")


async def fake_get_request(*args, **kwargs):
    """Return fake data."""
    payload = json.loads(load_fixture(f"{args[1]}.json"))
    return payload
