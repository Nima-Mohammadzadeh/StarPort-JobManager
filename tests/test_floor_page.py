"""Floor page for Zebra handhelds: encoder names, config, links."""


def client(jm):
    return jm.app.app.test_client()


def save(jm, **settings):
    r = client(jm).post("/api/settings", json=settings)
    assert r.status_code == 200
    return r.get_json()


# ── Task 1: encoder list and config ───────────────────────────────────────────
def test_clean_encoders_trims_dedupes_and_caps(jm):
    clean = jm.app.clean_encoders
    assert clean(" Sam, sam\nAnn;;  Lee   Ross \n") == "Sam\nAnn\nLee Ross"
    assert len(clean("\n".join(f"Name{i}" for i in range(50))).split("\n")) == 40
    assert clean("x" * 60) == "x" * 40


def test_encoders_default_empty_and_saved_through_settings(jm):
    assert jm.app.get_settings()["encoders"] == ""
    save(jm, encoders="Sam\nAnn")
    assert jm.app.get_settings()["encoders"] == "Sam\nAnn"


def test_floor_config_lists_encoders_and_printers(jm):
    cfg = client(jm).get("/api/floor/config").get_json()
    assert cfg["encoders"] == []
    save(jm, encoders="Sam\nAnn")
    cfg = client(jm).get("/api/floor/config").get_json()
    assert cfg["encoders"] == ["Sam", "Ann"]
    assert cfg["printers"] == ["Printer 1", "Printer 2", "Printer 3", "Printer 4"]
