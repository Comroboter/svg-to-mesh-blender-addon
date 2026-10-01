"""Update the add-on from its GitHub releases (stdlib only, no Blender).

Nothing here runs on its own: the add-on only contacts GitHub when the user
presses "Check for Updates" or "Install Update".
"""

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from .ai_client import _ssl_context

REPO = "comroboter/svg-to-mesh-blender-addon"
API_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_URL = "https://github.com/%s/releases" % REPO
PACKAGE = "svg_to_mesh"
MAX_ZIP_BYTES = 20 * 1024 * 1024
ALLOWED_HOSTS = ("github.com", "api.github.com", "codeload.github.com")
ALLOWED_SUFFIX = ".githubusercontent.com"  # release downloads redirect there


class UpdateError(Exception):
    pass


def parse_version(text):
    """'v1.2.3' / '1.2.3' -> (1, 2, 3); None if it is not a version."""
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)\s*$", str(text or ""))
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def _host_allowed(url):
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host in ALLOWED_HOSTS or host.endswith(ALLOWED_SUFFIX))


def _get(url, timeout, limit, accept="application/json"):
    if not _host_allowed(url):
        raise UpdateError("Refusing to download from %s" % url)
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "svg-to-mesh-updater"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            if not _host_allowed(resp.geturl()):
                raise UpdateError("Download was redirected to an unexpected host")
            data = resp.read(limit + 1)
    except urllib.error.HTTPError as ex:
        if ex.code in (403, 429):
            raise UpdateError("GitHub rate limit reached - try again later") from None
        if ex.code == 404:
            raise UpdateError("No release found on GitHub") from None
        raise UpdateError("GitHub answered with HTTP %d" % ex.code) from None
    except (urllib.error.URLError, OSError) as ex:
        reason = getattr(ex, "reason", ex)
        raise UpdateError("Could not reach GitHub: %s" % reason) from None
    if len(data) > limit:
        raise UpdateError("Download is unexpectedly large")
    return data


def parse_release(data):
    """Pick version, ZIP asset and notes from a GitHub release JSON object."""
    version = parse_version(data.get("tag_name"))
    if version is None:
        raise UpdateError("The latest release has no version tag")
    assets = [a for a in data.get("assets") or () if str(a.get("name", "")).endswith(".zip")]
    asset = next((a for a in assets if str(a.get("name", "")).startswith(PACKAGE)), assets[0] if assets else None)
    if asset is None:
        raise UpdateError("The latest release has no add-on ZIP")
    digest = str(asset.get("digest") or "")
    return {
        "version": version,
        "tag": data.get("tag_name"),
        "url": asset.get("browser_download_url", ""),
        "size": int(asset.get("size") or 0),
        "sha256": digest[7:].lower() if digest.startswith("sha256:") else None,
        "notes": str(data.get("body") or ""),
        "page": data.get("html_url") or RELEASES_URL,
    }


def latest_release(timeout=15):
    data = _get(API_URL, timeout, 1024 * 1024, "application/vnd.github+json")
    try:
        return parse_release(json.loads(data.decode("utf-8")))
    except (ValueError, AttributeError) as ex:
        raise UpdateError("Unexpected answer from GitHub (%s)" % ex) from None


def download(release, timeout=60):
    data = _get(release["url"], timeout, MAX_ZIP_BYTES, "application/octet-stream")
    if release.get("sha256") and hashlib.sha256(data).hexdigest() != release["sha256"]:
        raise UpdateError("Download is corrupted (checksum mismatch)")
    return data


def _members(zf):
    """Validated (member, path inside the package) pairs of an add-on ZIP."""
    result = []
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        parts = name.split("/")
        if name.startswith("/") or ".." in parts or ":" in parts[0] or parts[0] != PACKAGE:
            raise UpdateError("Unexpected file in the update: %s" % info.filename)
        rel = "/".join(parts[1:])
        if rel and not name.endswith("/"):
            result.append((info, rel))
    return result


def check_zip(data):
    """Validate an add-on ZIP. Returns (ZipFile, members, version)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UpdateError("Download is not a valid ZIP file") from None
    members = _members(zf)
    if sum(info.file_size for info, _rel in members) > 5 * MAX_ZIP_BYTES:
        raise UpdateError("Update is unexpectedly large")
    names = {rel for _info, rel in members}
    if "__init__.py" not in names or "blender_manifest.toml" not in names:
        raise UpdateError("The ZIP does not contain the add-on")
    manifest = zf.read(PACKAGE + "/blender_manifest.toml").decode("utf-8", "replace")
    m_id = re.search(r'^id\s*=\s*"([^"]+)"', manifest, re.M)
    m_ver = re.search(r'^version\s*=\s*"([^"]+)"', manifest, re.M)
    if not m_id or m_id.group(1) != PACKAGE or not m_ver or parse_version(m_ver.group(1)) is None:
        raise UpdateError("The ZIP contains a different add-on")
    return zf, members, parse_version(m_ver.group(1))


def is_dev_checkout(addon_dir):
    return os.path.exists(os.path.join(os.path.dirname(os.path.abspath(addon_dir)), ".git"))


def install_zip(data, addon_dir):
    """Write the add-on files from *data* (ZIP bytes) over *addon_dir*.

    Files are first extracted to a temporary folder next to the add-on, then
    copied over the installed ones; modules the new version no longer has are
    removed. The new code is used after restarting Blender. Returns the new
    version tuple.
    """
    zf, members, version = check_zip(data)
    addon_dir = os.path.abspath(addon_dir)
    if is_dev_checkout(addon_dir):
        raise UpdateError("This add-on runs from a git checkout - update it with git pull")
    staging = tempfile.mkdtemp(prefix=".svg_to_mesh-update-", dir=os.path.dirname(addon_dir))
    try:
        for info, rel in members:
            target = os.path.join(staging, *rel.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        new_files = {rel for _info, rel in members}
        for rel in sorted(new_files):
            target = os.path.join(addon_dir, *rel.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(os.path.join(staging, *rel.split("/")), target)
        for base, dirs, files in os.walk(addon_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                path = os.path.join(base, name)
                rel = os.path.relpath(path, addon_dir).replace(os.sep, "/")
                if rel not in new_files and name.endswith((".py", ".png", ".toml")):
                    try:
                        os.remove(path)
                    except OSError:
                        pass  # a leftover module is harmless
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return version
