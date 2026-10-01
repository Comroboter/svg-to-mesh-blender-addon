"""Build an installable add-on ZIP: ``python build.py`` -> dist/svg_to_mesh-<version>.zip

The ZIP contains the ``svg_to_mesh`` folder, so it can be installed both as
an extension (Blender 4.2+, "Install from Disk") and as a legacy add-on
(Blender 3.6 - 4.1, Preferences > Add-ons > Install).
"""

import os
import re
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
PKG = "svg_to_mesh"


def main():
    with open(os.path.join(ROOT, PKG, "blender_manifest.toml"), encoding="utf-8") as fh:
        version = re.search(r'^version\s*=\s*"([^"]+)"', fh.read(), re.M).group(1)
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    out = os.path.join(ROOT, "dist", "%s-%s.zip" % (PKG, version))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, dirs, files in os.walk(os.path.join(ROOT, PKG)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in sorted(files):
                if name.endswith((".pyc", ".pyo")):
                    continue
                path = os.path.join(base, name)
                zf.write(path, os.path.relpath(path, ROOT))
    print(out)


if __name__ == "__main__":
    main()
