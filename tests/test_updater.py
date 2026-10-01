"""Tests for the GitHub updater (offline: no network access needed)."""

import io
import os
import zipfile

import pytest

from svg_to_mesh.core import updater

MANIFEST = 'schema_version = "1.0.0"\nid = "%s"\nversion = "%s"\n'


def make_zip(version="9.0.0", ident="svg_to_mesh", extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("svg_to_mesh/__init__.py", "VERSION = %r\n" % version)
        zf.writestr("svg_to_mesh/blender_manifest.toml", MANIFEST % (ident, version))
        zf.writestr("svg_to_mesh/core/new_module.py", "")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_versions():
    assert updater.parse_version("v1.10.2") == (1, 10, 2)
    assert updater.parse_version("1.3") == (1, 3)
    assert updater.parse_version("v1.3.0") > updater.parse_version("1.2.9")
    assert updater.parse_version("latest") is None


def test_parse_release_picks_zip_and_digest():
    rel = updater.parse_release({
        "tag_name": "v1.3.0", "html_url": "https://github.com/x/y/releases/tag/v1.3.0", "body": "notes",
        "assets": [
            {"name": "readme.txt", "browser_download_url": "https://github.com/a"},
            {"name": "svg_to_mesh-1.3.0.zip", "browser_download_url": "https://github.com/b.zip",
             "size": 10, "digest": "sha256:ABCD"},
        ],
    })
    assert rel["version"] == (1, 3, 0)
    assert rel["url"] == "https://github.com/b.zip"
    assert rel["sha256"] == "abcd"
    with pytest.raises(updater.UpdateError):
        updater.parse_release({"tag_name": "v1.3.0", "assets": []})


def test_only_github_https_hosts_are_allowed():
    assert updater._host_allowed("https://github.com/a/b/releases/download/v1/x.zip")
    assert updater._host_allowed("https://release-assets.githubusercontent.com/x")
    assert not updater._host_allowed("http://github.com/x")
    assert not updater._host_allowed("https://github.com.evil.example/x")
    assert not updater._host_allowed("https://evilgithubusercontent.com/x")


@pytest.mark.parametrize("extra", [{"../evil.py": "x"}, {"/abs.py": "x"}, {"other/x.py": "x"},
                                   {"svg_to_mesh/../../x.py": "x"}])
def test_zip_with_foreign_paths_is_rejected(extra):
    with pytest.raises(updater.UpdateError):
        updater.check_zip(make_zip(extra=extra))


def test_zip_of_another_addon_is_rejected():
    with pytest.raises(updater.UpdateError):
        updater.check_zip(make_zip(ident="something_else"))
    with pytest.raises(updater.UpdateError):
        updater.check_zip(b"not a zip")


def test_install_replaces_files_and_removes_stale_modules(tmp_path):
    addon = tmp_path / "addons" / "svg_to_mesh"
    (addon / "core").mkdir(parents=True)
    (addon / "__init__.py").write_text("VERSION = 'old'\n")
    (addon / "removed_module.py").write_text("")
    (addon / "user_notes.txt").write_text("keep me")
    assert updater.install_zip(make_zip("9.1.0"), str(addon)) == (9, 1, 0)
    assert (addon / "__init__.py").read_text() == "VERSION = '9.1.0'\n"
    assert (addon / "core" / "new_module.py").exists()
    assert not (addon / "removed_module.py").exists()
    assert (addon / "user_notes.txt").exists()
    assert sorted(os.listdir(tmp_path / "addons")) == ["svg_to_mesh"]  # staging folder cleaned up


def test_install_refuses_git_checkouts(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "svg_to_mesh").mkdir()
    with pytest.raises(updater.UpdateError):
        updater.install_zip(make_zip(), str(tmp_path / "svg_to_mesh"))


def test_checksum_mismatch_is_rejected(monkeypatch):
    monkeypatch.setattr(updater, "_get", lambda *a, **k: b"data")
    with pytest.raises(updater.UpdateError):
        updater.download({"url": "https://github.com/x.zip", "sha256": "00"})
    assert updater.download({"url": "https://github.com/x.zip", "sha256": None}) == b"data"
