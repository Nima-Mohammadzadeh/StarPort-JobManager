"""
Demo sandbox for looking at the UI. Never touches the real local.db, serial registry,
job folders or backups: everything lives under mock_demo/site/.

    python mock_demo/run_mock.py            # http://127.0.0.1:5051
    python mock_demo/run_mock.py --reset    # throw the demo data away and re-seed

Code, templates and proofing/ are copied fresh on every start, so edits to templates/index.html show up
after a restart. Demo data is only seeded when the demo database doesn't exist yet.
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site")
PORT = 5051

if "--reset" in sys.argv and os.path.isdir(SITE):
    shutil.rmtree(SITE)
os.makedirs(SITE, exist_ok=True)

for f in os.listdir(ROOT):
    if f.endswith(".py"):
        shutil.copy2(os.path.join(ROOT, f), SITE)
shutil.rmtree(os.path.join(SITE, "templates"), ignore_errors=True)
shutil.copytree(os.path.join(ROOT, "templates"), os.path.join(SITE, "templates"))
shutil.rmtree(os.path.join(SITE, "proofing"), ignore_errors=True)
shutil.copytree(os.path.join(ROOT, "proofing"), os.path.join(SITE, "proofing"),
                ignore=shutil.ignore_patterns("__pycache__"))

for d in ("customers", "serial_registry", "backups"):
    os.makedirs(os.path.join(SITE, d), exist_ok=True)
with open(os.path.join(SITE, "config.ini"), "w", encoding="utf-8") as fh:
    fh.write(f"""[paths]
database        = {os.path.join(SITE, 'demo.db')}
serial_registry = {os.path.join(SITE, 'serial_registry')}
z_drive_base    = {os.path.join(SITE, 'customers')}

[proofing]
data_dir        = {os.path.join(SITE, 'proofing_data')}

[server]
host = 127.0.0.1
port = {PORT}
""")

env = dict(os.environ, JM_NO_BROWSER="1")
if not os.path.exists(os.path.join(SITE, "demo.db")):
    subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_demo.py"), SITE],
                   cwd=SITE, env=env, check=True)
if not os.path.isdir(os.path.join(SITE, "Master Templates")):      # mock BarTender templates
    subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_templates.py"), SITE],
                   cwd=SITE, env=env, check=True)
os.chdir(SITE)
sys.exit(subprocess.call([sys.executable, "app.py"], env=env))
