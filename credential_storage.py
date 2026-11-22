"""Private storage for expert-credential documents (licences, degree certificates).

These are sensitive identity-adjacent documents, so:
  * the bucket is PRIVATE, size-limited and restricted to PDF/JPEG/PNG;
  * files are never served by URL - an admin reviewer gets a short-lived signed
    URL generated per view;
  * files are deleted as soon as a reviewer has made a decision, when the
    contractor removes the credential, or when they delete their account.

The bucket is created by this app's own code on first use (using the service-role
client), not by hand-editing the production database. Local disk is never used:
Render's filesystem doesn't survive redeploys (see CLAUDE.md).
"""
import logging
import uuid

from website.supabase_client import supabase
from website.expert_verification import MAX_DOCUMENT_BYTES, DOCUMENT_MIME_TYPES

logger = logging.getLogger(__name__)

CREDENTIAL_BUCKET = "expert-credentials"
_bucket_ready = False


def ensure_bucket():
    """Create the private bucket if it doesn't exist. Safe to call repeatedly."""
    global _bucket_ready
    if _bucket_ready:
        return
    try:
        supabase.storage.get_bucket(CREDENTIAL_BUCKET)
    except Exception:
        try:
            supabase.storage.create_bucket(CREDENTIAL_BUCKET, options={
                "public": False,
                "file_size_limit": MAX_DOCUMENT_BYTES,
                "allowed_mime_types": list(DOCUMENT_MIME_TYPES),
            })
            logger.info("Created private storage bucket %s", CREDENTIAL_BUCKET)
        except Exception:
            # Lost a race with another worker creating it, or a real failure -
            # the upload that follows surfaces a real failure either way.
            logger.warning("Could not create bucket %s (may already exist)", CREDENTIAL_BUCKET, exc_info=True)
    _bucket_ready = True


def upload_credential_file(user_id, data, extension, mime):
    """Store a document; returns its storage path (never a URL)."""
    ensure_bucket()
    path = f"{int(user_id)}/{uuid.uuid4().hex}.{extension}"
    supabase.storage.from_(CREDENTIAL_BUCKET).upload(path, data, {"content-type": mime, "upsert": "false"})
    return path


def signed_credential_url(path, expires_in=120):
    result = supabase.storage.from_(CREDENTIAL_BUCKET).create_signed_url(path, expires_in)
    return result.get("signedURL") or result.get("signedUrl")


def delete_credential_file(path):
    """Best-effort delete. Returns True if the delete call succeeded."""
    if not path:
        return True
    try:
        supabase.storage.from_(CREDENTIAL_BUCKET).remove([path])
        return True
    except Exception:
        logger.exception("Could not delete credential file %s", path)
        return False
