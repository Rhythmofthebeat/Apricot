import httpx

_API = "https://api.daily.co/v1"


def _headers(api_key):
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def create_room(api_key):
    """Create a Daily.co room, with cloud recording enabled if the account's
    plan supports it. Returns room dict with 'name' and 'url'.

    Cloud recording is a paid-plan feature - requesting it on an account
    without that plan makes Daily reject room creation entirely with a 400
    ("property 'enable_recording' cannot be set to that value with your
    current plan"), which broke every meeting room, not just recording.
    Falls back to a plain room (no recording) so meetings always work
    regardless of plan tier.
    """
    with httpx.Client(timeout=10) as client:
        r = client.post(
            f"{_API}/rooms",
            headers=_headers(api_key),
            json={"privacy": "public", "properties": {"enable_recording": "cloud", "max_participants": 10}},
        )
        if r.status_code == 400 and "enable_recording" in r.text:
            r = client.post(
                f"{_API}/rooms",
                headers=_headers(api_key),
                json={"privacy": "public", "properties": {"max_participants": 10}},
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
