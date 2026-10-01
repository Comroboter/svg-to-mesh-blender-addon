"""Print the GitHub release notes for a version: its CHANGELOG section plus install steps.

Usage: python tools/release_notes.py 1.1.1 owner/repo
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def changelog_section(version):
    lines = open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8").read().splitlines()
    out, inside = [], False
    for line in lines:
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip() == version
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def main():
    version, repo = sys.argv[1], sys.argv[2]
    notes = changelog_section(version)
    print("Import SVG files and trace images (logos) into clean, manifold meshes that are ready for booleans.")
    print()
    if notes:
        print("## What's new")
        print(notes)
        print()
    print("## Installation")
    print("1. Download **svg_to_mesh-%s.zip** below (do not unzip it)." % version)
    print("2. **Blender 4.2 and newer:** *Edit > Preferences > Get Extensions*, open the drop-down menu in the "
          "top right corner and choose *Install from Disk...*")
    print("   **Blender 3.6 to 4.1:** *Edit > Preferences > Add-ons > Install...*, then enable the add-on.")
    print("3. Open the 3D Viewport sidebar (**N**), tab **SVG Mesh**, or use *File > Import*.")
    print()
    print("See the [README](https://github.com/%s#readme) for usage and options." % repo)


if __name__ == "__main__":
    main()
