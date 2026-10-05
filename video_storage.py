"""Candidate interview video storage via Supabase Storage.

Interview videos live on local disk like everything else in uploads/ and
audio/, which Render's filesystem doesn't durably persist across deploys
or restarts (see CLAUDE.md) - fine for the small, disposable audio/TTS
files already there, but not for interview recordings candidates and
employers actually rely on later. This stores them in a private Supabase
Storage bucket instead, accessed via short-lived signed URLs generated
at playback time rather than stored directly, since signed URLs expire.
"""
import logging
import uuid

import httpx

from website.supabase_client import supabase, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

logger = logging.getLogger(__name__)

INTERVIEW_VIDEO_BUCKET = "interview-videos"
MEETING_RECORDING_BUCKET = "meeting-recordings"
VIDEO_SCAN_BUCKET = "video-scan-uploads"

_EXT_BY_CONTENT_TYPE = {
    "video/webm": "webm", "audio/webm": "webm",
    "video/mp4": "mp4", "audio/mp4": "mp4",
    "video/ogg": "ogg", "audio/ogg": "ogg",
}


def _ext_for(content_type):
    """Safari (desktop and iOS) doesn't support WebM at all and records mp4
    instead - storing every upload under a hardcoded .webm path/content-type
    regardless of what was actually recorded breaks playback in whatever
    browser didn't record WebM, since browsers trust the stored content-type
    over sniffing the bytes for a <video>/<audio> element."""
    return _EXT_BY_CONTENT_TYPE.get((content_type or "").split(";")[0].strip().lower(), "webm")


def upload_interview_video(application_id, turn_number, video_bytes, content_type="video/webm"):
    """Uploads a candidate's recorded video answer. Returns the storage path
    (not a URL) - callers should store this and call get_signed_video_url()
    fresh each time the video is actually played back."""
    path = f"{application_id}/{turn_number}.{_ext_for(content_type)}"
    supabase.storage.from_(INTERVIEW_VIDEO_BUCKET).upload(
        path, video_bytes, {"content-type": content_type or "video/webm", "upsert": "true"},
    )
    return path


def get_signed_video_url(path, expires_in=300, bucket=INTERVIEW_VIDEO_BUCKET):
    """Short-lived signed URL for playback - regenerated on every request
    rather than cached, so it's never exposed for longer than needed."""
    result = supabase.storage.from_(bucket).create_signed_url(path, expires_in)
    return result.get("signedURL") or result.get("signedUrl")


def upload_meeting_recording(room_id, role, video_bytes, content_type="video/webm"):
    """Uploads a participant's own local-camera recording of a live meeting
    (see meeting_room.html) - one file per room per role, overwritten if they
    rejoin. Returns the storage path, not a URL."""
    path = f"{room_id}/{role}.{_ext_for(content_type)}"
    supabase.storage.from_(MEETING_RECORDING_BUCKET).upload(
        path, video_bytes, {"content-type": content_type or "video/webm", "upsert": "true"},
    )
    return path


def get_signed_meeting_recording_url(path, expires_in=300):
    return get_signed_video_url(path, expires_in, bucket=MEETING_RECORDING_BUCKET)


def delete_interview_videos(application_id):
    """Best-effort cleanup of every video under an application - called when
    an application/job/company is deleted, or a candidate deletes their
    account, so recordings don't outlive the record that references them."""
    try:
        files = supabase.storage.from_(INTERVIEW_VIDEO_BUCKET).list(str(application_id))
        paths = [f"{application_id}/{f['name']}" for f in files]
        if paths:
            supabase.storage.from_(INTERVIEW_VIDEO_BUCKET).remove(paths)
    except Exception:
        logger.exception(f"Failed to clean up interview videos for application {application_id}")


def create_scan_upload_url(extension):
    """One-time signed upload URL for the standalone resume job-match page's
    video scan (app.py's /resume-jobmatch/video-feedback) - the browser PUTs
    the raw video straight to Supabase Storage with this URL, using only the
    public anon key, which bypasses our own app's request path for the large
    transfer entirely. That matters because Render's edge rejects large
    request bodies sent to this app's own domain well before reaching
    gunicorn - a signed upload URL sidesteps that since the upload never
    touches avants.ai at all, only Supabase's own endpoint. Returns
    (upload_url, path) - path is what the browser reports back afterward so
    the server can fetch and then delete the same object."""
    path = f"scans/{uuid.uuid4().hex}{extension}"
    signed = supabase.storage.from_(VIDEO_SCAN_BUCKET).create_signed_upload_url(path)
    return signed["signed_url"], path


def download_scan_to_file(path, dest_path):
    """Streams a video scan straight to disk for processing - the
    service-role client makes this an outbound request from our server to
    Supabase, not an inbound one through Render's edge, so it isn't subject
    to the same body-size limit that direct uploads to our own domain hit.

    Streams in chunks rather than using the SDK's plain .download(), which
    loads the entire file into memory as one bytes object first - for a
    multi-hundred-MB video that risks OOM-killing the single gunicorn worker
    process this whole site runs on, taking every visitor down with it, not
    just the one request."""
    url = f"{SUPABASE_URL}/storage/v1/object/{VIDEO_SCAN_BUCKET}/{path}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
    }
    with httpx.stream("GET", url, headers=headers, timeout=300) as response:
        response.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)


def delete_scan(path):
    """Best-effort cleanup - a scan's video is never meant to be retained
    once it's been transcribed, only the transcript/coaching report are."""
    try:
        supabase.storage.from_(VIDEO_SCAN_BUCKET).remove([path])
    except Exception:
        logger.exception(f"Failed to delete video scan upload at {path}")
