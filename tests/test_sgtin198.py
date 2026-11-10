"""SGTIN-198 encoder, scheme table and SGTIN-96 regression tests."""
import os
import sys

import pytest

UPC = "012345678905"

# Captured from the pre-change generate_epc (SGTIN-96) so its output stays byte-for-byte identical.
SGTIN96_LITERALS = [
    ("012345678905", 12345, "303400C0E4424C8000003039"),
    ("036000291452", 1, "30340232801C764000000001"),
    ("999999999991", 274877906943, "30343D08FC61A7FFFFFFFFFF"),
]


def mod(jm):
    return sys.modules["generators"]


def bits_of(hexstr):
    return format(int(hexstr, 16), "0%db" % (len(hexstr) * 4))


def build198(upc, text):
    b = ("00110110" + "001" + "101" + format(int("0" + upc[:6]), "024b") + format(int(upc[6:11]), "020b"))
    s = "".join(format(ord(c), "07b") for c in text).ljust(140, "0")
    b += s + "00"
    return format(int(b, 2), "050X")


def test_sgtin96_unchanged(jm):
    g = mod(jm)
    for upc, n, expected in SGTIN96_LITERALS:
        assert g.generate_epc(upc, n) == expected
        assert g.generate_epc(upc, n, "sgtin96") == expected


def test_sgtin198_layout(jm):
    g = mod(jm)
    epc = g.generate_epc(UPC, 123, "sgtin198")
    assert len(epc) == 50 and epc == epc.upper()
    b = bits_of(epc)
    assert b[0:8] == "00110110"
    assert b[8:11] == "001"
    assert b[11:14] == "101"
    assert b[14:38] == format(int("0" + UPC[:6]), "024b")
    assert b[38:58] == format(int(UPC[6:11]), "020b")
    assert b[58:65] == "0110001"
    assert b[65:72] == format(ord("2"), "07b")
    assert b[72:79] == format(ord("3"), "07b")
    assert set(b[79:198]) == {"0"}
    assert b[198:200] == "00"


def test_sgtin198_tds_example(jm):
    # The GS1 TDS worked example could not be fetched here (network blocked), so the expected
    # value is constructed independently from the documented bit layout (build198 above).
    g = mod(jm)
    upc, serial = "614141234567", 32
    assert g.generate_epc(upc, serial, "sgtin198") == build198(upc, "32")
    assert g.generate_epc(upc, serial, "sgtin198").startswith("36")


def test_zero_padding_vs_digit_zero(jm):
    g = mod(jm)
    a, b = g.generate_epc(UPC, 5, "sgtin198"), g.generate_epc(UPC, 50, "sgtin198")
    assert a != b
    assert g.decode_epc(a)["serial"] == 5
    assert g.decode_epc(b)["serial"] == 50


def test_roundtrip(jm):
    g = mod(jm)
    serials = [0, 1] + [10 ** d - 1 for d in range(1, 21)] + [10 ** d for d in range(1, 20)]
    serials += [int(("987654321" * 3)[: 1 + k % 20]) for k in range(200)]
    assert len(serials) >= 200
    for s in serials:
        assert g.decode_epc(g.generate_epc(UPC, s, "sgtin198")) == {
            "scheme": "sgtin198", "upc": UPC, "serial": s, "serial_text": str(s)}
    for s in [0, 1, 12345, 2 ** 38 - 1]:
        assert g.decode_epc(g.generate_epc(UPC, s)) == {
            "scheme": "sgtin96", "upc": UPC, "serial": s, "serial_text": str(s)}


def test_serial_limits(jm):
    g = mod(jm)
    big = 10 ** 20 - 1
    assert g.decode_epc(g.generate_epc(UPC, big, "sgtin198"))["serial"] == big
    with pytest.raises(ValueError, match="20"):
        g.format_serial(10 ** 20)
    with pytest.raises(ValueError):
        g.format_serial(-1)
    with pytest.raises(ValueError):
        g.format_serial(1, "prefix")
    with pytest.raises(ValueError):
        g.generate_epc(UPC, 1, "nope")


def test_non_numeric_198_serial(jm):
    g = mod(jm)
    d = g.decode_epc(build198(UPC, "AB12"))
    assert d["serial"] is None and d["serial_text"] == "AB12"
    assert d["scheme"] == "sgtin198" and d["upc"] == UPC


def test_decode_rejects_bad_input(jm):
    g = mod(jm)
    for bad in ["30" + "0" * 20, "3A" + "0" * 22, "30" + "0" * 48]:
        with pytest.raises(ValueError):
            g.decode_epc(bad)


def test_decoders_check_partition(jm):
    g = mod(jm)
    good96 = g.generate_epc(UPC, 1)
    bad96 = format(int(bits_of(good96)[:11] + "100" + bits_of(good96)[14:], 2), "024X")
    with pytest.raises(ValueError):
        g.decode_epc(bad96)
    good198 = g.generate_epc(UPC, 1, "sgtin198")
    bad198 = format(int(bits_of(good198)[:11] + "100" + bits_of(good198)[14:], 2), "050X")
    with pytest.raises(ValueError):
        g.decode_epc(bad198)


def test_scheme_helpers(jm):
    g = mod(jm)
    assert g.SERIAL_FORMATS == ("number",)
    assert g.scheme_label("sgtin96") == "SGTIN-96" and g.scheme_label("sgtin198") == "SGTIN-198"
    assert g.epc_scheme_of(g.generate_epc(UPC, 1)) == "sgtin96"
    assert g.epc_scheme_of(g.generate_epc(UPC, 1, "sgtin198")) == "sgtin198"
    assert g.epc_scheme_of("3A" + "0" * 22) is None
    assert g.epc_scheme_of("30" + "0" * 48) is None


def _read_rows(path):
    if path.endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(path)
        try:
            return [tuple(r) for r in wb.active.iter_rows(min_row=2, values_only=True)]
        finally:
            wb.close()
    import csv
    with open(path, newline="", encoding="utf-8") as fh:
        return [(int(a), b, c) for a, b, c in list(csv.reader(fh))[1:]]


def test_roll_plan_198(jm):
    g = mod(jm)
    rolls = g.roll_plan(UPC, 1000, 120, 50, scheme="sgtin198")
    assert len(rolls) == 3
    assert rolls[0]["epc_start"] == g.generate_epc(UPC, 1000, "sgtin198")
    assert rolls[0]["epc_end"] == g.generate_epc(UPC, 1049, "sgtin198")
    assert all(len(r["epc_start"]) == 50 and len(r["epc_end"]) == 50 for r in rolls)


def test_db_files_198(jm, tmp_path):
    g = mod(jm)
    res = g.generate_db_files(str(tmp_path), UPC, 1000, 2500, 1000, scheme="sgtin198")
    assert res["file_count"] == 3
    serials = []
    for p in res["files"]:
        for serial, upc, epc in _read_rows(p):
            assert isinstance(epc, str) and len(epc) == 50
            assert g.decode_epc(epc)["serial"] == serial
            serials.append(serial)
    assert serials == list(range(1000, 3500))


def test_db_files_96_unchanged(jm, tmp_path):
    g = mod(jm)
    res = g.generate_db_files(str(tmp_path), UPC, 1000, 2500, 1000)
    n = 1000
    for p in res["files"]:
        for serial, upc, epc in _read_rows(p):
            assert serial == n and len(epc) == 24 and epc == g.generate_epc(UPC, n)
            n += 1
    assert n == 3500


def test_verify_db_file_catches_tampering(jm, tmp_path):
    g = mod(jm)
    res = g.generate_db_files(str(tmp_path), UPC, 1000, 5, None, scheme="sgtin198")
    path = res["files"][0]
    g.verify_db_file(path, UPC, 1000, 5, "sgtin198")
    from openpyxl import load_workbook
    wb = load_workbook(path)
    wb.active.cell(row=3, column=3, value=g.generate_epc(UPC, 9999, "sgtin198"))
    wb.save(path)
    with pytest.raises(ValueError) as e:
        g.verify_db_file(path, UPC, 1000, 5, "sgtin198")
    assert os.path.basename(path) in str(e.value) and "row 3" in str(e.value)
