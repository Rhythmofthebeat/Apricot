# supabase_client.py
import os
from supabase import create_client
from supabase.lib.client_options import SyncClientOptions

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
# optional anon key if you need client-side flows
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY env vars")

# create a server/admin client for server-side tasks (use service_role key)
# Options must be a SyncClientOptions instance with snake_case fields - a
# raw dict (or the old camelCase keys) fails at Client.__init__ time, since
# that's what current supabase-py's create_client() actually expects despite
# older examples showing a plain dict.
supabase = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY,
    options=SyncClientOptions(
        auto_refresh_token=False,
        persist_session=False,
    ),
)
