"""Nightly database backup: dumps every table to gzipped JSON and uploads it to
Google Drive, so employer/candidate data survives even if the Supabase project
itself were ever lost, not just individual rows.

Runs as its own scheduled process (see the `avants-db-backup` cron service in
render.yaml) - separate from the web app, no Flask app context needed.

Strictly read-only against the database: every table is read with a plain
SELECT, nothing is ever written, updated, or deleted here. Restoring from a
backup is a deliberate, separate, reviewed action - not something this script
does automatically.
"""
import gzip
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from dotenv import load_dotenv
from sqlalchemy import MetaData, create_engine

load_dotenv(override=False)

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
BACKUP_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_BACKUP_FOLDER_ID")
RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "30"))
BACKUP_PREFIX = "avants_backup_"


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _database_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    # Render/Heroku-style URLs sometimes use the old "postgres://" scheme,
    # which SQLAlchemy 1.4+ rejects - mirrors website/__init__.py's fixup.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def dump_database():
    engine = create_engine(_database_url())
    metadata = MetaData()
    metadata.reflect(bind=engine)

    dump = {}
    with engine.connect() as conn:
        for table_name, table in metadata.tables.items():
            rows = conn.execute(table.select()).mappings().all()
            dump[table_name] = [dict(row) for row in rows]
            print(f"  {table_name}: {len(rows)} rows")
    engine.dispose()
    return dump


def _google_drive_credentials():
    """Mirrors app.py's _google_tts_credentials(): prefer raw service-account
    JSON pasted into an env var over a file path, since Render Secret Files
    have repeatedly failed to land on disk in production for this app."""
    from google.oauth2 import service_account

    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if raw:
        info = json.loads(raw)
        return service_account.Credentials.from_service_account_info(info, scopes=DRIVE_SCOPES)

    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path:
        return service_account.Credentials.from_service_account_file(path, scopes=DRIVE_SCOPES)

    raise RuntimeError(
        "No Google credentials configured "
        "(set GOOGLE_CREDENTIALS_JSON or GOOGLE_APPLICATION_CREDENTIALS)"
    )


def upload_to_drive(local_path, filename):
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    if not BACKUP_FOLDER_ID:
        raise RuntimeError("GOOGLE_DRIVE_BACKUP_FOLDER_ID is not set")

    creds = _google_drive_credentials()
    service = build("drive", "v3", credentials=creds)
    media = MediaFileUpload(local_path, mimetype="application/gzip", resumable=False)
    file_metadata = {"name": filename, "parents": [BACKUP_FOLDER_ID]}
    uploaded = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    print(f"  uploaded as Drive file id {uploaded['id']}")
    return service


def prune_old_backups(service):
    if RETENTION_DAYS <= 0:
        return
    cutoff = datetime.now(timezone.utc).timestamp() - RETENTION_DAYS * 86400
    query = f"'{BACKUP_FOLDER_ID}' in parents and name contains '{BACKUP_PREFIX}' and trashed = false"
    results = service.files().list(q=query, fields="files(id, name, createdTime)").execute()
    for f in results.get("files", []):
        created = datetime.fromisoformat(f["createdTime"].replace("Z", "+00:00")).timestamp()
        if created < cutoff:
            service.files().delete(fileId=f["id"]).execute()
            print(f"  pruned old backup {f['name']}")


def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting database backup...")
    dump = dump_database()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{BACKUP_PREFIX}{timestamp}.json.gz"
    local_path = f"/tmp/{filename}"

    payload = json.dumps(dump, default=_json_default).encode("utf-8")
    with gzip.open(local_path, "wb") as f:
        f.write(payload)
    print(f"  wrote {local_path} ({len(payload)} bytes uncompressed, {os.path.getsize(local_path)} gzipped)")

    service = upload_to_drive(local_path, filename)
    prune_old_backups(service)
    os.remove(local_path)
    print("Backup complete.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"BACKUP FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
