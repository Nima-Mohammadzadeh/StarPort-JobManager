"""Seeds mock BarTender templates for the sandbox: fake .btw files in a Master Templates folder
under mock_demo/site/, indexed through the app's own sync, with preview images and a few jobs that
already used one. The files are random bytes, not real BarTender documents.

    python mock_demo/seed_templates.py <site folder>      # run_mock.py does this when the folder is missing
"""
import io
import os
import random
import struct
import sys
import zlib

site = sys.argv[1]
sys.path.insert(0, site)
import app as A  # noqa: E402  (reads the sandbox config.ini next to it)

A.validate_and_prepare()
A.init_db(create=True)                                               # adds any new columns to an older demo database
MASTER = os.path.join(site, "Master Templates")
if os.path.realpath(MASTER).lower().startswith(os.path.realpath(r"Z:\\").lower()):
    sys.exit("Refusing to seed templates onto the Z: drive")

CUSTOMERS = ["Acme Apparel", "Northwind Retail", "Contoso Footwear", "Fabrikam Goods", "Tailspin Outdoor",
             "Globex Sportswear", "Umbrella Pharma", "Wayne Logistics", "Stark Electronics", "Hooli Home"]
UPCS = ["012345678905", "036000291452", "042100005264", "049000028911", "070847811169", "078742013459",
        "012000001017", "041220576743", "072250037562", "038000138416", "016000275683", "028400090858"]
DESCRIPTIONS = ['1x2 care label', '2x3 hang tag', '4x2 carton label', '3x3 shelf label', '2x1 price tag']
ACCENTS = [(15, 118, 110), (94, 72, 168), (179, 50, 43), (154, 91, 0), (42, 116, 48), (40, 90, 160)]


def png(w, h, pix):
    raw = b"".join(b"\x00" + bytes(c for px in pix[y * w:(y + 1) * w] for c in px) for y in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def preview(seed):
    """A small label-like picture: coloured header, text bars, a barcode and an antenna squiggle."""
    rnd = random.Random(seed)
    w, h = 240, 120
    pix = [(255, 255, 255)] * (w * h)
    acc = rnd.choice(ACCENTS)

    def rect(x0, y0, x1, y1, c):
        for y in range(max(0, y0), min(h, y1)):
            for x in range(max(0, x0), min(w, x1)):
                pix[y * w + x] = c
    rect(0, 0, w, 1, (170, 190, 195)); rect(0, h - 1, w, h, (170, 190, 195))
    rect(0, 0, 1, h, (170, 190, 195)); rect(w - 1, 0, w, h, (170, 190, 195))
    rect(8, 8, w - 8, 30, acc)
    for i in range(3):
        rect(10, 40 + i * 12, 10 + rnd.randint(70, 130), 46 + i * 12, (80, 100, 105))
    x = 12
    while x < 140:
        bw = rnd.choice([1, 2, 3])
        rect(x, 84, x + bw, 108, (20, 30, 35)); x += bw + rnd.choice([1, 2, 3])
    for i in range(12):                                   # antenna
        rect(160 + i * 6, 80 + (14 if i % 2 else 0), 164 + i * 6, 92 + (14 if i % 2 else 0), acc)
    return png(w, h, pix)


def fake_btw(seed, kb):
    return b"MOCK-BTW\x00" + random.Random(seed).randbytes(kb * 1024)


def put(rel, seed, kb):
    path = os.path.join(MASTER, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(fake_btw(seed, kb))
    return path


rnd = random.Random(11)
n = 0
for ci, cust in enumerate(CUSTOMERS):
    for upc in rnd.sample(UPCS, rnd.randint(2, 4)):
        n += 1; put(f"{cust}/{cust} - {upc}.btw", n, rnd.randint(40, 700))
    for desc in rnd.sample(DESCRIPTIONS, rnd.randint(0, 2)):
        n += 1; put(f"{cust}/{cust} - {desc}.btw", n, rnd.randint(40, 700))
for upc in UPCS[:2]:                                      # an older subfolder, to show nesting
    n += 1; put(f"Acme Apparel/Archive 2024/Acme Apparel - {upc} (old layout).btw", n, 220)
for name in ("Blank 4x2 RFID master", "Blank 2x3 hang tag master", "Blank 1x2 care label master"):
    n += 1; put(f"Generic/{name}.btw", n, 120)
n += 1; put("Needs sorting - Initech sample.btw", n, 90)            # a top-level file
os.makedirs(os.path.join(MASTER, "Hooli Home", "2026 artwork"), exist_ok=True)   # an empty folder

c = A.app.test_client()
c.post("/api/settings", json={"btw_template_dir": MASTER})
r = c.post("/api/templates/sync").get_json()
print("templates indexed:", r)

rows = c.get("/api/templates?per=200").get_json()["templates"]
for t in rows:                                            # most, but not all, have a preview
    if t["id"] % 5 != 0:
        c.post(f"/api/templates/{t['id']}/thumbnail", data={"file": (io.BytesIO(preview(t["id"])), "p.png")},
               content_type="multipart/form-data")

conn = A.get_db()
jobs = [dict(r) for r in conn.execute("SELECT id, customer FROM jobs WHERE folder_path IS NOT NULL ORDER BY id LIMIT 14")]
conn.close()
for j in jobs:
    mine = [t for t in rows if t["name"].startswith(j["customer"])]
    if mine:
        c.post(f"/api/jobs/{j['id']}/template", json={"template_id": mine[j["id"] % len(mine)]["id"]})
print("mock templates ready under", MASTER)
