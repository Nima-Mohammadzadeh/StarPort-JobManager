"""Seeds the demo database through the app's own API (test client), so the data is realistic."""
import sys

site = sys.argv[1]
sys.path.insert(0, site)
import app as A  # noqa: E402  (reads the sandbox config.ini next to it)

A.validate_and_prepare()
A.init_db()
A.serial_startup()
c = A.app.test_client()


def post(url, body, **kw):
    r = c.post(url, json=body, **kw)
    if r.status_code >= 400:
        print("FAILED", url, r.status_code, r.get_json())
    return r.get_json()


JOBS = [  # customer, title, ticket, upc, qty, lpr, size, inlay, due
    ("Acme Apparel", "Care label woven", "40121", "012345678905", 5000, 500, '1" x 2"', "Dogbone", "10/31/26"),
    ("Northwind Retail", "Hang tag RFID", "40133", "036000291452", 12000, 1000, '2" x 3"', "Monza R6", "11/03/26"),
    ("Contoso Footwear", "Shoe box label", "40140", "042100005264", 3000, 500, '4" x 2"', "Dogbone", "11/05/26"),
    ("Fabrikam Goods", "Size sticker", "40152", "012345678905", 1500, 500, '1" x 1"', "Monza 4QT", "11/07/26"),
    ("Tailspin Outdoor", "Price tag", "40161", "049000028911", 800, 400, '2" x 1"', "Dogbone", "11/10/26"),
]
ids = []
for cust, title, tk, upc, qty, lpr, size, inlay, due in JOBS:
    r = post("/api/jobs", {"customer": cust, "title": title, "job_ticket": tk, "upc": upc, "quantity": qty,
                           "lpr": lpr, "label_size": size, "inlay_type": inlay, "due_date": due,
                           "notes": "Ship partials daily." if tk == "40133" else ""})
    ids.append((r.get("job") or {}).get("id") or r.get("id"))
    print("created", tk, r.get("job", r).get("job_number") if isinstance(r.get("job", r), dict) else r)

# A special job with no UPC.
r = post("/api/jobs", {"job_type": "special", "customer": "Initech", "title": "Sample run", "job_ticket": "40170",
                       "quantity": 600, "lpr": 200, "label_size": '3" x 3"'})
ids.append((r.get("job") or {}).get("id") or r.get("id"))


def roll(j, n, status, who, note=""):
    post(f"/api/jobs/{j}/rolls/{n}", {"status": status, "operator": who, "note": note})


def note(j, body, who, rn=None, imp=False):
    post(f"/api/jobs/{j}/notes", {"body": body, "author": who, "roll_number": rn, "important": imp})


def status(j, s, reason="", why=""):
    body = {"status": s, "operator": "Dana"}
    if s == "On Hold":                       # a job only goes on hold with a reason
        body.update(hold_reason=reason or "Other", hold_note=why or "Waiting on the planner")
    r = c.patch(f"/api/jobs/{j}", json=body)
    if r.status_code >= 400:
        print("FAILED status", j, s, r.get_json())


# Job 1: in progress, mixed roll states.
a = ids[0]
status(a, "In Progress")
for n in (1, 2, 3):
    roll(a, n, "complete", "Sam")
roll(a, 4, "running", "Priya", "Stopped at label 180, printer jam")
note(a, "Inlay supplier changed lot mid-job. Keep lots separate.", "Dana", imp=True)
note(a, "Jam cleared, resumed after lunch.", "Priya", rn=4)

# Job 2: in progress, 12 rolls so the list scrolls.
b = ids[1]
status(b, "In Progress")
for n in range(1, 9):
    roll(b, n, "complete", "Sam" if n < 6 else "Priya")
roll(b, 9, "running", "Sam")
roll(b, 10, "running", "Priya")
note(b, "Customer wants first and last tag of each roll photographed.", "Dana", imp=True)

def printer(j, n, name, who):
    post(f"/api/jobs/{j}/rolls/{n}/printer", {"printer": name, "operator": who})


# Running rolls are on printers (an encoder picks one when loading a roll).
printer(a, 4, "Printer 1", "Priya")
printer(b, 9, "Printer 2", "Sam")
printer(b, 10, "Printer 3", "Priya")

# Job 3: on hold. Job 4: all rolls done (shows the Mark complete prompt). Job 5, 6: queued.
status(ids[2], "In Progress"); roll(ids[2], 1, "complete", "Sam")
status(ids[2], "On Hold", "Customer", "Waiting on new artwork approval")
note(ids[2], "Waiting on new artwork approval.", "Dana", imp=True)
status(ids[3], "In Progress")
for n in (1, 2, 3):
    roll(ids[3], n, "complete", "Priya")
# Bulk jobs so grouping, folding and scrolling can be judged with a realistic pile.
import datetime
import random
random.seed(7)
customers = ["Acme Apparel", "Northwind Retail", "Contoso Footwear", "Fabrikam Goods", "Tailspin Outdoor",
             "Globex Sportswear", "Umbrella Pharma", "Wayne Logistics", "Stark Electronics", "Hooli Home"]
upcs = ["012345678905", "036000291452", "042100005264", "049000028911", "070847811169", "078742013459"]
today = datetime.date.today()
for i in range(28):
    cust = random.choice(customers)
    due = today + datetime.timedelta(days=random.randint(-4, 25))
    qty = random.choice([800, 1500, 3000, 5000, 9000])
    lpr = random.choice([400, 500, 1000])
    r = post("/api/jobs", {"customer": cust, "title": random.choice(["Care label", "Hang tag", "Size sticker", "Shelf label", "Carton label"]),
                           "job_ticket": str(41000 + i), "upc": random.choice(upcs), "quantity": qty, "lpr": lpr,
                           "label_size": random.choice(['1" x 2"', '2" x 3"', '4" x 2"']), "inlay_type": random.choice(["Dogbone", "Monza R6"]),
                           "due_date": due.strftime("%m/%d/%y")})
    jid = (r.get("job") or {}).get("id") or r.get("id")
    k = i % 5
    if k in (0, 1) and jid:
        status(jid, "In Progress")
        for n in range(1, random.randint(2, 4)):
            roll(jid, n, "complete", random.choice(["Sam", "Priya"]))
    elif k == 2 and jid:
        status(jid, "On Hold", *random.choice([("Material", "Waiting on Monza R6 inlays"), ("Printer", "Printer 3 head replacement"),
                                                ("Data / file", "UPC list from the customer doesn't match the ticket"),
                                                ("Customer", "Customer asked to pause the order")]))
    elif k == 3 and jid:
        status(jid, "In Progress")
        for n in range(1, qty // lpr + 1):
            roll(jid, n, "complete", "Sam")
        status(jid, "Complete")

# Needs-attention scenarios that need time to pass, set straight in the demo database:
# Acme roll 4 was started yesterday afternoon and never finished; one bulk job was put on hold
# before hold reasons existed, so it has none.
yday = (datetime.datetime.now() - datetime.timedelta(hours=19)).strftime("%Y-%m-%d %H:%M:%S")
conn = A.get_db()
conn.execute("UPDATE job_rolls SET started_at=? WHERE job_id=? AND roll_number=4", (yday, ids[0]))
legacy = conn.execute("SELECT id FROM jobs WHERE job_ticket='41007'").fetchone()
if legacy:
    conn.execute("UPDATE jobs SET status='On Hold', hold_reason=NULL, hold_note=NULL, hold_by=NULL, hold_since=? WHERE id=?",
                 ((datetime.datetime.now() - datetime.timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"), legacy["id"]))
conn.commit(); conn.close()
print("seeded", ids)
