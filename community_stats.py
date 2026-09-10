"""Persistent aggregate for successful Community Edition resume tool runs."""
from sqlalchemy import text

BASELINE = 5234


def initialize_counter(engine):
    # One transaction prevents exposing an unprotected table on PostgreSQL.
    with engine.begin() as conn:
        conn.execute(text('''CREATE TABLE IF NOT EXISTS community_resume_counter (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            total BIGINT NOT NULL CHECK (total >= 0)
        )'''))
        if engine.dialect.name == 'postgresql':
            conn.execute(text('ALTER TABLE community_resume_counter ENABLE ROW LEVEL SECURITY'))
            for role in ('anon', 'authenticated'):
                if conn.execute(text('SELECT 1 FROM pg_roles WHERE rolname = :role'), {'role': role}).scalar():
                    conn.execute(text(f'REVOKE ALL ON community_resume_counter FROM {role}'))
        conn.execute(text('INSERT INTO community_resume_counter (id, total) VALUES (1, :baseline) ON CONFLICT (id) DO NOTHING'), {'baseline': BASELINE})


def resume_total(engine):
    with engine.connect() as conn:
        return conn.execute(text('SELECT total FROM community_resume_counter WHERE id = 1')).scalar_one()


def record_resume_processed(engine):
    # Database arithmetic avoids lost increments across concurrent workers.
    with engine.begin() as conn:
        conn.execute(text('UPDATE community_resume_counter SET total = total + 1 WHERE id = 1'))
