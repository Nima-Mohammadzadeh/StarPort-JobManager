"""
seed_serials.py — find serials you already used before Job Manager, so they are never reused
============================================================================================
For a UPC the registry has never seen, the first job starts at 26,000,000,001. If that UPC
ran before (in the old files, or before any app), those serials may already be on tags in
the field, and a new job could repeat them. This tool reads the old encoding databases in
your job folders, finds the highest serial used per UPC, and records it in the registry so
the next job starts after it.

    python seed_serials.py "Z:\\3 Encoding and Printing Files\\Customers Encoding Files"
        shows what it found and what it WOULD change. Nothing is written.
    python seed_serials.py "<folder>" --apply
        writes it. Forward-only: it never lowers anything, and running it twice is safe.

What it reads: .xlsx and .csv files that contain EPC values (24 hex characters, the
SGTIN-96 format this app writes) or Serial # and UPC columns. Everything else is ignored.
It reads files; it never changes them.
"""
import os
import re
import sys
import csv
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

MASK38 = (1 << 38) - 1
HEX24 = re.compile(r"^[0-9A-Fa-f]{24}$")
UPC12 = re.compile(r"(?<!\d)(\d{12})(?!\d)")


def _rows(path):
    """Yield rows (lists of cell values) from the first sheets of an xlsx, or a csv."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            for row in csv.reader(fh):
                yield row
    else:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            for ws in wb.worksheets[:3]:
                for row in ws.iter_rows(values_only=True):
                    yield row
        finally:
            wb.close()


def scan_file(path):
    """Returns {upc: (max_serial, rows)} for one file, or {} if it has no usable data."""
    from generators import decode_epc, upc_check_digit
    epc_col = serial_col = upc_col = None
    by_prefix, by_upc = {}, {}
    name_upc = next((m for m in UPC12.findall(os.path.basename(path))), None)
    seen = 0
    for row in _rows(path):
        seen += 1
        if seen <= 6 and epc_col is None and serial_col is None:      # look for headers / first data row
            for i, c in enumerate(row):
                t = str(c).strip().lower() if c is not None else ""
                if "epc" in t and epc_col is None:     epc_col = i
                elif "serial" in t and serial_col is None: serial_col = i
                elif t.startswith("upc") and upc_col is None: upc_col = i
            if epc_col is None and serial_col is None:
                for i, c in enumerate(row):
                    if isinstance(c, str) and HEX24.match(c.strip()) and c.strip().startswith("30"):
                        epc_col = i; break
        if epc_col is not None and epc_col < len(row):
            v = row[epc_col]
            if isinstance(v, str):
                v = v.strip()
                if HEX24.match(v) and v[:2] == "30":
                    n = int(v, 16)
                    prefix, serial = n >> 38, n & MASK38
                    cur = by_prefix.get(prefix)
                    if cur is None:
                        by_prefix[prefix] = [serial, 1, v]
                    else:
                        cur[0] = max(cur[0], serial); cur[1] += 1
        elif serial_col is not None and serial_col < len(row):
            try:
                serial = int(float(str(row[serial_col]).replace(",", "")))
            except (TypeError, ValueError):
                continue
            upc = None
            if upc_col is not None and upc_col < len(row):
                m = UPC12.search(str(row[upc_col]))
                upc = m.group(1) if m else None
            upc = upc or name_upc
            if upc:
                cur = by_upc.get(upc)
                if cur is None: by_upc[upc] = [serial, 1]
                else: cur[0] = max(cur[0], serial); cur[1] += 1
    out = {}
    for prefix, (serial, count, sample) in by_prefix.items():
        try:
            upc = decode_epc(sample)["upc"]
        except Exception:
            continue
        cur = out.get(upc)
        out[upc] = (max(serial, cur[0]) if cur else serial, (cur[1] if cur else 0) + count)
    for upc, (serial, count) in by_upc.items():
        cur = out.get(upc)
        out[upc] = (max(serial, cur[0]) if cur else serial, (cur[1] if cur else 0) + count)
    return out


def main():
    ap = argparse.ArgumentParser(description="Find serials already used in old job folders")
    ap.add_argument("folder")
    ap.add_argument("--apply", action="store_true", help="write the results to the serial registry")
    ap.add_argument("--max-files", type=int, default=0, help="stop after this many files (0 = all)")
    a = ap.parse_args()
    if not os.path.isdir(a.folder):
        sys.exit(f"Folder not found: {a.folder}")

    import config
    from serial_registry import peek_next_start, seed_last_used
    print("=" * 66)
    print("  Find serials already used" + ("" if a.apply else "   (PREVIEW: nothing is written)"))
    print("=" * 66)
    print(f"  Scanning : {a.folder}")
    print(f"  Registry : {config.SERIAL_REGISTRY_DIR}\n")

    found, files, usable, unreadable = {}, 0, 0, 0
    for dirpath, dirs, names in os.walk(a.folder):
        dirs[:] = [d for d in dirs if d.lower() not in ("serial_registry", "$recycle.bin")]
        for f in names:
            if f.startswith(("~$", ".")) or not f.lower().endswith((".xlsx", ".csv")):
                continue
            path = os.path.join(dirpath, f)
            files += 1
            if files % 25 == 0:
                print(f"  ... {files} files read", file=sys.stderr, flush=True)
            try:
                res = scan_file(path)
            except Exception:
                unreadable += 1
                continue
            if res:
                usable += 1
            for upc, (serial, rows) in res.items():
                cur = found.get(upc)
                if cur is None or serial > cur[0]:
                    found[upc] = (serial, path, rows)
            if a.max_files and files >= a.max_files:
                break
        if a.max_files and files >= a.max_files:
            break

    todo = []
    print(f"  {files} files read, {usable} had EPC / serial data, {unreadable} could not be read.")
    print(f"  {len(found)} UPCs found.\n")
    if found:
        print(f"  {'UPC':<14}{'Highest serial found':>22}{'Registry next':>20}   Result")
        for upc in sorted(found):
            serial, path, rows = found[upc]
            nxt = peek_next_start(upc)
            need = serial >= nxt
            if need:
                todo.append((upc, serial, path))
            print(f"  {upc:<14}{serial:>22,}{nxt:>20,}   " + ("RESERVE up to this serial" if need else "already safe"))
    print()
    if not todo:
        print("  Nothing to change: every UPC found is already covered by the registry.")
        return
    if not a.apply:
        print(f"  {len(todo)} UPC(s) would move forward. Run again with --apply to record them.")
        return
    for upc, serial, path in todo:
        r = seed_last_used(upc, serial, f"ran before this app, found in {os.path.basename(path)}")
        print(f"  {upc}: next serial is now {r['next_start']:,}")
    print(f"\n  Done. {len(todo)} UPC(s) updated.")


if __name__ == "__main__":
    main()
