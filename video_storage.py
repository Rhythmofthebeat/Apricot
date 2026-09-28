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

from website.supabase_client import supabase

logger = logging.getLogger(__name__)

INTERVIEW_VIDEO_BUCKET = "interview-videos"
MEETING_RECORDING_BUCKET = "meeting-recordings"


def upload_interview_video(application_id, turn_number, video_bytes):
    """Uploads a candidate's recorded video answer. Returns the storage path
    (not a URL) - callers should store this and call get_signed_video_url()
    fresh each time the video is actually played back."""
    path = f"{application_id}/{turn_number}.webm"
    supabase.storage.from_(INTERVIEW_VIDEO_BUCKET).upload(
        path, video_bytes, {"content-type": "video/webm", "upsert": "true"},
    )
    return path


def get_signed_video_url(path, expires_in=300, bucket=INTERVIEW_VIDEO_BUCKET):
    """Short-lived signed URL for playback - regenerated on every request
    rather than cached, so it's never exposed for longer than needed."""
    result = supabase.storage.from_(bucket).create_signed_url(path, expires_in)
    return result.get("signedURL") or result.get("signedUrl")


def upload_meeting_recording(room_id, role, video_bytes):
    """Uploads a participant's own local-camera recording of a live meeting
    (see meeting_room.html) - one file per room per role, overwritten if they
    rejoin. Returns the storage path, not a URL."""
    path = f"{room_id}/{role}.webm"
    supabase.storage.from_(MEETING_RECORDING_BUCKET).upload(
        path, video_bytes, {"content-type": "video/webm", "upsert": "true"},
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
