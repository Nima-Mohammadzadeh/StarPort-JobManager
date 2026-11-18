"""Build a code-only update folder for the host PC.

    python make_update.py            # from main
    python make_update.py <git ref>  # from another branch, tag or commit

Writes dist/JobManager-<version>-update/ with the app's code taken straight from git at that ref,
so nothing local can slip in: no database, no config.ini, no serial registry, no backups, no job
data, no tests or demo sandbox. Copying that folder's contents over the live Job Manager folder
replaces the code and leaves the live data alone. The script refuses to finish if a data file
would be in the folder.
"""
import io, os, re, shutil, subprocess, sys, tarfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))

# Only these ever go to the host PC.
SHIP_DIRS = ("templates/",)
SKIP_FILES = {".gitignore", "make_update.py"}

# Must never be in an update folder: they hold the live PC's data and settings.
DATA = re.compile(r"(^|/)(config\.ini|local\.db.*|.*\.db|.*\.db-(wal|shm|journal)|.*\.sqlite3?)$"
                  r"|(^|/)(serial_registry|backups|template_thumbnails|Master Templates|customers)(/|$)",
                  re.IGNORECASE)


def git(*args) -> bytes:
    return subprocess.run(["git", *args], cwd=HERE, check=True, capture_output=True).stdout


def shipped(name: str) -> bool:
    if name in SKIP_FILES or name.endswith("/"):
        return False
    return "/" not in name or name.startswith(SHIP_DIRS)


def main(ref="main"):
    commit = git("rev-parse", "--short", f"{ref}^{{commit}}").decode().strip()
    m = re.search(r'^APP_VERSION = "([^"]+)"', git("show", f"{ref}:app.py").decode(), re.M)
    if not m:
        raise SystemExit(f"[STOP] No APP_VERSION in app.py at {ref}")
    version = m.group(1)
    out = os.path.join(HERE, "dist", f"JobManager-{version}-update")
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)

    files = []
    with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", commit))) as tar:
        for member in tar.getmembers():
            if not member.isfile() or not shipped(member.name):
                continue
            if DATA.search(member.name):
                shutil.rmtree(out)
                raise SystemExit(f"[STOP] {member.name} looks like live data and must never be in an update")
            dest = os.path.join(out, *member.name.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with tar.extractfile(member) as src, open(dest, "wb") as f:
                shutil.copyfileobj(src, f)
            files.append(member.name)

    notes = f"""Job Manager {version} update (commit {commit}, built {datetime.now():%Y-%m-%d %H:%M}).

This folder holds CODE ONLY. It contains NO database, NO config.ini, NO serial registry,
NO backups and NO job data, so copying it over the live folder cannot overwrite them.

To install:
 1. Stop the running Job Manager.
 2. (Optional safety) Copy the live folder somewhere as a backup.
 3. Copy the CONTENTS of this folder into the live folder and choose
    "Replace files" for the code files. Do NOT delete the live folder first and do NOT
    use any "mirror" / "sync with delete" option.
 4. Start Job Manager again (run.bat). The window says "Job Manager {version} is running."
    Existing jobs, settings and serials are untouched.

Never copy your development or project folder itself: it can hold its own local.db,
config.ini and serial_registry, which WOULD replace the live ones. Only copy this folder.

Do not run new_install.bat on the live PC. (It refuses anyway when a database is there.)

Files that exist only on the live PC and are never in this folder:
 config.ini, local.db (or the database path in config.ini), serial_registry/, backups/,
 template_thumbnails/, Master Templates, job folders.

To roll back: copy your backup of the old code files over these ones.

Files in this update ({len(files)}):
""" + "".join(f"  {f}\n" for f in sorted(files))
    with open(os.path.join(out, "UPDATE-NOTES.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(notes)

    # Final check of what is actually on disk.
    for root, _, names in os.walk(out):
        for n in names:
            rel = os.path.relpath(os.path.join(root, n), out).replace(os.sep, "/")
            if DATA.search(rel):
                shutil.rmtree(out)
                raise SystemExit(f"[STOP] {rel} looks like live data; update folder removed")
    print(f"Job Manager {version} ({commit}): {len(files)} code files in {out}")
    print("No database, config.ini, serial registry or backups in it.")


if __name__ == "__main__":
    main(*sys.argv[1:2])
