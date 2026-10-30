from cod_region_filter import paths


def test_migrate_legacy_moves_both_data_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    old_local = tmp_path / "local" / "cod-region-filter"
    old_roaming = tmp_path / "roaming" / "cod-region-filter"
    old_local.mkdir(parents=True)
    old_roaming.mkdir(parents=True)
    (old_local / "catalog.db").write_bytes(b"catalog")
    (old_roaming / "policy.yaml").write_text("mode: allowlist\n", encoding="utf-8")

    paths.migrate_legacy()

    assert (tmp_path / "local" / "cod-ping-king" / "catalog.db").read_bytes() == b"catalog"
    assert (tmp_path / "roaming" / "cod-ping-king" / "policy.yaml").exists()
    assert not old_local.exists()


def test_migrate_legacy_leaves_existing_new_folder_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    new = tmp_path / "local" / "cod-ping-king"
    old = tmp_path / "local" / "cod-region-filter"
    new.mkdir(parents=True)
    old.mkdir(parents=True)
    (new / "catalog.db").write_bytes(b"new")
    (old / "catalog.db").write_bytes(b"old")

    paths.migrate_legacy()

    assert (new / "catalog.db").read_bytes() == b"new"
    assert old.exists()
