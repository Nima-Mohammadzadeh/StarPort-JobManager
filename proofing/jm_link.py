"""
proofing/jm_link.py — which proof belongs to which Job Manager job
==================================================================
The proof records themselves are the proofing app's own .rfidproof.json files (in the
proofing data folder). This table is only an index so the Jobs list and the job drawer can
show a proof's state without opening files:

    job_proofs  one row per proof file, keyed by the proof's stable key (the random part
                of its Proof ID, e.g. PRF-20260916-REV1-DAAD7FC2 -> DAAD7FC2)

Each proof file also carries jm_job_number (JOB-####), which never changes, so the index
can always be rebuilt from the files (rebuild_from_files). Job ids can change when a backup
is restored with --keep-new-work; job numbers can't. There is deliberately no foreign key
to jobs: deleting a job record must never fail because a proof points at it.

No Pillow / reportlab imports here, so init_db() works even if proofing can't load.
"""
import re

SCHEMA = """CREATE TABLE IF NOT EXISTS job_proofs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proof_key TEXT NOT NULL UNIQUE,
    job_id INTEGER,
    job_number TEXT,
    path TEXT NOT NULL,
    proof_id TEXT, proof_revision TEXT, approval_status TEXT, lifecycle_status TEXT,
    last_generated_at TEXT, output_path TEXT, customer TEXT, ticket TEXT,
    location TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""

# Shown on the Jobs list: the state of the newest non-archived proof of each job.
PROOF_SUMMARY_SQL = """(SELECT p.approval_status FROM job_proofs p WHERE p.job_id=j.id AND p.location!='archive'
                        ORDER BY p.updated_at DESC LIMIT 1) AS proof_status,
                       (SELECT p.lifecycle_status FROM job_proofs p WHERE p.job_id=j.id AND p.location!='archive'
                        ORDER BY p.updated_at DESC LIMIT 1) AS proof_lifecycle"""


def init_schema(cur):
    cur.execute(SCHEMA)
    cur.execute("CREATE INDEX IF NOT EXISTS ix_job_proofs_job ON job_proofs(job_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_job_proofs_number ON job_proofs(job_number)")


def proof_key(proof_id: str) -> str:
    """Stable key from a Proof ID: the 8-hex part survives every revision."""
    m = re.search(r"([0-9A-F]{8})$", str(proof_id or "").strip().upper())
    return m.group(1) if m else ""


def location_of(path: str) -> str:
    p = str(path or "").replace("\\", "/").lower()
    if "/jobs/archive/" in p:
        return "archive"
    if "/jobs/approved/" in p:
        return "approved"
    return "active"


def upsert(conn, now, job, path, job_row=None):
    """Record (or update) one proof. job is a ProofJob; job_row the linked jobs row or None.
    A proof with no Job Manager link is still indexed (job_id NULL) so relinking can find it."""
    key = proof_key(job.proof_id)
    if not key:
        return None
    jid = job_row["id"] if job_row else None
    jnum = job_row["job_number"] if job_row else (job.jm_job_number or None)
    vals = (jid, jnum, str(path), job.proof_id, str(job.proof_revision or ""), job.approval_status,
            job.lifecycle_status, job.last_generated_at, job.last_generated_output_path,
            job.customer, job.job_number, location_of(path), now)
    cur = conn.execute("""UPDATE job_proofs SET job_id=?, job_number=?, path=?, proof_id=?, proof_revision=?,
        approval_status=?, lifecycle_status=?, last_generated_at=?, output_path=?, customer=?, ticket=?,
        location=?, updated_at=? WHERE proof_key=?""", vals + (key,))
    if not cur.rowcount:
        conn.execute("""INSERT INTO job_proofs (job_id, job_number, path, proof_id, proof_revision, approval_status,
            lifecycle_status, last_generated_at, output_path, customer, ticket, location, updated_at, proof_key, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", vals + (key, now))
    return key


def move(conn, now, old_path, new_path):
    conn.execute("UPDATE job_proofs SET path=?, location=?, updated_at=? WHERE path=?",
                 (str(new_path), location_of(new_path), now, str(old_path)))


def forget(conn, path):
    conn.execute("DELETE FROM job_proofs WHERE path=?", (str(path),))


def for_job(conn, job_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM job_proofs WHERE job_id=? ORDER BY (location='archive'), updated_at DESC", (job_id,))]


def by_path(conn):
    """{path: row} for annotating the proof queue with the Job Manager job."""
    return {r["path"]: dict(r) for r in conn.execute(
        """SELECT p.path, p.job_id, p.job_number, j.job_ticket, j.customer AS job_customer, j.status AS job_status
           FROM job_proofs p LEFT JOIN jobs j ON j.id = p.job_id""")}


def find_job(conn, job_id=None, job_number=None):
    """The jobs row a proof points at. The job number decides (it never changes); the id is
    only used when there is no number."""
    row = None
    if job_number:
        row = conn.execute("SELECT * FROM jobs WHERE job_number=?", (job_number,)).fetchone()
    if row is None and job_id and not job_number:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (int(job_id),)).fetchone()
    return dict(row) if row else None


def jobs_for_ticket(conn, ticket, customer=None):
    """Open or archived jobs (not cancelled) with this ticket, newest first."""
    t = str(ticket or "").strip().lower()
    if not t:
        return []
    rows = [dict(r) for r in conn.execute(
        """SELECT id, job_number, job_ticket, customer, title, status, upc, archived_at FROM jobs
           WHERE LOWER(TRIM(job_ticket))=? AND status!='Cancelled' ORDER BY id DESC""", (t,))]
    if customer:
        c = re.sub(r"[^a-z0-9]", "", customer.lower())
        same = [r for r in rows if re.sub(r"[^a-z0-9]", "", (r["customer"] or "").lower()) == c]
        if same:
            return same
    return rows
