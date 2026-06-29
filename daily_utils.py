import httpx

_API = "https://api.daily.co/v1"


def _headers(api_key):
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def create_room(api_key):
    """Create a Daily.co room with cloud recording enabled. Returns room dict with 'name' and 'url'."""
    with httpx.Client(timeout=10) as client:
        r = client.post(
            f"{_API}/rooms",
            headers=_headers(api_key),
            json={"privacy": "public", "properties": {"enable_recording": "cloud", "max_participants": 10}},
        )
        r.raise_for_status()
        return r.json()


def get_recordings(api_key, room_name):
    """Return list of recording objects for a given room."""
    with httpx.Client(timeout=10) as client:
        r = client.get(
            f"{_API}/recordings",
            headers=_headers(api_key),
            params={"room_name": room_name},
        )
        r.raise_for_status()
        return r.json().get("data", [])


def get_recording_link(api_key, recording_id):
    """Return a short-lived download URL for a recording."""
    with httpx.Client(timeout=10) as client:
        r = client.get(
            f"{_API}/recordings/{recording_id}/access-link",
            headers=_headers(api_key),
        )
        r.raise_for_status()
        return r.json().get("download_link")
