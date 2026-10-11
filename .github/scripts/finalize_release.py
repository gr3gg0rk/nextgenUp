#!/usr/bin/env python3
"""Tidy a NextGenUp GitHub release after tauri-action has uploaded every bundle.

GitHub lists release assets alphabetically, and tauri-action's names
(aarch64, amd64, x86_64, .sig files...) made people download the wrong
file. This step leaves only the installers people should click, named
after the computer they are for, and keeps auto-update working:

- installers are renamed (renaming keeps their download counts)
- macOS .app.tar.gz updater bundles and the .deb move to the permanent
  `updater-packages` pre-release (never shown as "latest")
- .sig files are deleted: their signatures are already inside latest.json
- latest.json is rewritten to point at the final locations
- a per-OS download table is put at the top of the release notes

Usage (CI, after all build jobs):  python finalize_release.py v1.2.1
Needs `gh` authenticated via GH_TOKEN; GH_REPO selects the repository.
"""
import json
import os
import subprocess
import sys
import tempfile

REPO = os.environ.get("GH_REPO", "riponcm/nextgenUp")
UPDATER_TAG = "updater-packages"
DL = f"https://github.com/{REPO}/releases/download/"

# suffix of tauri-action's file name -> public name
RENAME = [
    ("-setup.exe", "NextGenUp-Windows-Setup.exe"),
    (".msi", "NextGenUp-Windows_MSI-alternative.msi"),
    ("aarch64.dmg", "NextGenUp-Mac-AppleSilicon.dmg"),
    ("x64.dmg", "NextGenUp-Mac-Intel.dmg"),
    (".AppImage", "NextGenUp-Linux.AppImage"),
    (".rpm", "NextGenUp-Linux_Fedora-RedHat.rpm"),
]
MOVE_SUFFIXES = (".app.tar.gz", ".deb")


def gh(*args, stdin=None):
    return subprocess.run(["gh", *args], check=True, text=True,
                          capture_output=True, input=stdin).stdout


def api(path, method="GET", **fields):
    cmd = ["api", "-X", method, path]
    for k, v in fields.items():
        cmd += ["-f", f"{k}={v}"]
    out = gh(*cmd)
    return json.loads(out) if out.strip() else None


def classify(name):
    if name == "latest.json":
        return "manifest", None
    if name.endswith(".sig"):
        return "delete", None
    for suffix, public in RENAME:
        if name.endswith(suffix):
            return "rename", public
    if name.endswith(MOVE_SUFFIXES):
        return "move", None
    return "keep", None


def ensure_updater_release():
    try:
        api(f"repos/{REPO}/releases/tags/{UPDATER_TAG}")
    except subprocess.CalledProcessError:
        gh("release", "create", UPDATER_TAG, "--repo", REPO, "--prerelease",
           "--target", "main",
           "--title", "Auto-update packages (not for download)",
           "--notes", "These files are used by NextGenUp's built-in auto-updater. "
           f"To install NextGenUp, go to https://github.com/{REPO}/releases/latest")


def plan_moves(tag, version, assets):
    """Decide every action and every final URL without touching anything."""
    plan, final = [], {}
    taken = set()
    for name, asset in sorted(assets.items()):
        kind, public = classify(name)
        old = f"{DL}{tag}/{name}"
        if kind == "rename":
            if public in taken:
                sys.exit(f"two assets would both be named {public}; refusing to guess")
            taken.add(public)
            final[old] = f"{DL}{tag}/{public}"
        elif kind == "move":
            public = name if version in name else name.replace("NextGenUp_", f"NextGenUp_{version}_", 1)
            final[old] = f"{DL}{UPDATER_TAG}/{public}"
        plan.append((kind, name, asset, public))
    return plan, final


def main(tag, dry_run=False):
    version = tag.lstrip("v")
    rel = api(f"repos/{REPO}/releases/tags/{tag}")
    assets = {a["name"]: a for a in rel["assets"]}
    if "latest.json" not in assets:
        sys.exit("latest.json is missing from the release; did every build job finish?")

    tmp = tempfile.mkdtemp()
    gh("release", "download", tag, "--repo", REPO, "-p", "latest.json", "-D", tmp, "--clobber")
    path = os.path.join(tmp, "latest.json")
    with open(path) as f:
        manifest = json.load(f)
    plats = manifest["platforms"]

    plan, final = plan_moves(tag, version, assets)

    # Validate before changing anything: every updater entry must resolve
    unresolved = [k for k, p in plats.items() if p["url"] not in final]
    if unresolved:
        sys.exit("latest.json entries this script does not know how to map, "
                 f"nothing was changed: {unresolved}")
    for p in plats.values():
        p["url"] = final[p["url"]]
    if "windows-x86_64-nsis" in plats:  # generic key -> the installer most people use
        plats["windows-x86_64"] = dict(plats["windows-x86_64-nsis"])

    for kind, name, _, public in plan:
        print(f"{kind:8} {name}" + (f" -> {public}" if public else ""))
    for k, p in plats.items():
        print(f"manifest {k} -> {p['url'].split('/download/')[1]}")
    if dry_run:
        print("dry run: nothing changed")
        return

    ensure_updater_release()
    for kind, name, asset, public in plan:
        if kind == "rename" and public != name:
            api(f"repos/{REPO}/releases/assets/{asset['id']}", "PATCH", name=public)
        elif kind == "move":
            gh("release", "download", tag, "--repo", REPO, "-p", name, "-D", tmp, "--clobber")
            if public != name:
                os.replace(os.path.join(tmp, name), os.path.join(tmp, public))
            gh("release", "upload", UPDATER_TAG, "--repo", REPO, "--clobber", os.path.join(tmp, public))
            api(f"repos/{REPO}/releases/assets/{asset['id']}", "DELETE")
        elif kind == "delete":
            api(f"repos/{REPO}/releases/assets/{asset['id']}", "DELETE")

    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)
    gh("release", "upload", tag, "--repo", REPO, "--clobber", path)

    # Download table at the top of the notes
    body = rel.get("body") or ""
    if "NextGenUp-Windows-Setup.exe" not in body:
        table = (
            "| Your computer | Download |\n|---|---|\n"
            f"| **Windows** 10 / 11 | [NextGenUp-Windows-Setup.exe]({DL}{tag}/NextGenUp-Windows-Setup.exe) |\n"
            f"| **Mac** with Apple Silicon (M1, M2, M3, M4) | [NextGenUp-Mac-AppleSilicon.dmg]({DL}{tag}/NextGenUp-Mac-AppleSilicon.dmg) |\n"
            f"| **Mac** with Intel (2019 and earlier) | [NextGenUp-Mac-Intel.dmg]({DL}{tag}/NextGenUp-Mac-Intel.dmg) |\n"
            f"| **Linux** | [NextGenUp-Linux.AppImage]({DL}{tag}/NextGenUp-Linux.AppImage) |\n\n"
        )
        gh("release", "edit", tag, "--repo", REPO, "--notes-file", "-", stdin=table + body)
    print("done")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    if len(args) != 1 or not args[0].startswith("v"):
        sys.exit("usage: finalize_release.py vX.Y.Z [--dry-run]")
    main(args[0], dry_run="--dry-run" in sys.argv)
