"""Nightly backup: dumps every database table to gzipped JSON, archives the
locally-stored uploaded files (resumes, company logos, hire-evidence
screenshots, interview audio), and uploads both to Google Drive - so
employer/candidate data survives even if the Supabase project itself were
ever lost, not just individual rows.

Runs as its own scheduled process (see the `avants-db-backup` cron service in
render.yaml) - separate from the web app, no Flask app context needed.

Strictly read-only against the database and the local upload/audio folders:
every table is read with a plain SELECT and files are only read, never
written, moved, or deleted. Restoring from a backup is a deliberate, separate,
reviewed action - not something this script does automatically.
"""
import gzip
import json
import os
import sys
import tarfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from dotenv import load_dotenv
from sqlalchemy import MetaData, create_engine

load_dotenv(override=False)

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIRS = [
    ("uploads", os.path.join(REPO_DIR, "uploads")),
    ("audio", os.path.join(REPO_DIR, "audio")),
]

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
BACKUP_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_BACKUP_FOLDER_ID")
RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "30"))
DB_BACKUP_PREFIX = "avants_backup_"
FILES_BACKUP_PREFIX = "avants_files_"


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
    # which SQLAlchemy 1.4+ rejects - mirrors website/__init__.py's fixup,
    # including forcing the psycopg2 driver explicitly (see that file for why).
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
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


def archive_uploaded_files(local_path):
    """Tars up whatever's currently in uploads/ and audio/. Returns True if
    anything was written, False if both folders were empty (nothing to back
    up, skip the upload)."""
    wrote_any = False
    with tarfile.open(local_path, "w:gz") as tar:
        for arcname, dir_path in UPLOAD_DIRS:
            if not os.path.isdir(dir_path):
                continue
            entries = os.listdir(dir_path)
            if not entries:
                continue
            tar.add(dir_path, arcname=arcname)
            wrote_any = True
            print(f"  {arcname}: {len(entries)} entries archived")
    return wrote_any


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


def _drive_service():
    from googleapiclient.discovery import build

    if not BACKUP_FOLDER_ID:
        raise RuntimeError("GOOGLE_DRIVE_BACKUP_FOLDER_ID is not set")
    return build("drive", "v3", credentials=_google_drive_credentials())


def upload_to_drive(service, local_path, filename):
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(local_path, mimetype="application/gzip", resumable=False)
    file_metadata = {"name": filename, "parents": [BACKUP_FOLDER_ID]}
    uploaded = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    print(f"  uploaded as Drive file id {uploaded['id']}")


def prune_old_backups(service, prefix):
    if RETENTION_DAYS <= 0:
        return
    cutoff = datetime.now(timezone.utc).timestamp() - RETENTION_DAYS * 86400
    query = f"'{BACKUP_FOLDER_ID}' in parents and name contains '{prefix}' and trashed = false"
    results = service.files().list(q=query, fields="files(id, name, createdTime)").execute()
    for f in results.get("files", []):
        created = datetime.fromisoformat(f["createdTime"].replace("Z", "+00:00")).timestamp()
        if created < cutoff:
            service.files().delete(fileId=f["id"]).execute()
            print(f"  pruned old backup {f['name']}")


def main():
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    service = _drive_service()

    print(f"[{datetime.now(timezone.utc).isoformat()}] Backing up database...")
    dump = dump_database()
    db_filename = f"{DB_BACKUP_PREFIX}{timestamp}.json.gz"
    db_local_path = f"/tmp/{db_filename}"
    payload = json.dumps(dump, default=_json_default).encode("utf-8")
    with gzip.open(db_local_path, "wb") as f:
        f.write(payload)
    print(f"  wrote {db_local_path} ({len(payload)} bytes uncompressed, {os.path.getsize(db_local_path)} gzipped)")
    upload_to_drive(service, db_local_path, db_filename)
    prune_old_backups(service, DB_BACKUP_PREFIX)
    os.remove(db_local_path)

    print("Backing up uploaded files...")
    files_filename = f"{FILES_BACKUP_PREFIX}{timestamp}.tar.gz"
    files_local_path = f"/tmp/{files_filename}"
    if archive_uploaded_files(files_local_path):
        print(f"  wrote {files_local_path} ({os.path.getsize(files_local_path)} bytes gzipped)")
        upload_to_drive(service, files_local_path, files_filename)
        prune_old_backups(service, FILES_BACKUP_PREFIX)
    else:
        print("  nothing to archive, skipped")
    if os.path.exists(files_local_path):
        os.remove(files_local_path)

    print("Backup complete.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"BACKUP FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
