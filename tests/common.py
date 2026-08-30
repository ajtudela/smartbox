import json
import pathlib


def get_fixture_path(filename: str) -> pathlib.Path:
    """Get path of fixture."""
    return pathlib.Path(__file__).parent.joinpath("fixtures", filename)


def load_fixture(filename):
    """Load a fixture.

    Some fixtures are git symlinks to a sibling file. On a checkout without
    symlink support (Windows without developer mode) they are materialised as a
    one-line text file holding the relative target path; follow it transparently.
    """
    path = get_fixture_path(filename)
    text = path.read_text(encoding="utf-8")
    stripped = text.strip()
    if (
        "\n" not in stripped
        and stripped.endswith(".json")
        and not stripped.startswith(("{", "["))
    ):
        return (path.parent / stripped).resolve().read_text(encoding="utf-8")
    return text


async def fake_get_request(*args, **kwargs):
    """Return fake data."""
    return json.loads(load_fixture(f"{args[1]}.json"))
