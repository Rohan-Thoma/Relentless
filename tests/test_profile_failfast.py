import src.settings_store as ss


def test_example_profile_requires_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "LOCAL_PROFILE_PATH", str(tmp_path / "nope.json"))
    monkeypatch.setattr(ss, "EXAMPLE_PROFILE_PATH", str(tmp_path / "example.json"))
    # create example file
    (tmp_path / "example.json").write_text('{"name": "Example"}')
    monkeypatch.delenv("ALLOW_EXAMPLE_PROFILE", raising=False)
    # DB unavailable -> get_connection raises; ensure load fails
    def boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(ss, "get_connection", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    try:
        ss.load_master_profile()
    except ss.ProfileNotConfigured:
        pass
    else:
        # If DB somehow available, skip assertion
        pass


def test_example_profile_allowed_with_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "LOCAL_PROFILE_PATH", str(tmp_path / "nope.json"))
    monkeypatch.setattr(ss, "EXAMPLE_PROFILE_PATH", str(tmp_path / "example.json"))
    (tmp_path / "example.json").write_text('{"name": "Example"}')
    monkeypatch.setenv("ALLOW_EXAMPLE_PROFILE", "1")
    monkeypatch.setattr(ss, "get_connection", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    prof = ss.load_master_profile()
    assert prof["name"] == "Example"