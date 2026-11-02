#!/usr/bin/env python3
"""Package the local browser component without profiles, state or credentials."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

PROJECT = Path(__file__).resolve().parent.parent
FILES = ("manifest.json", "rules.js", "background.js", "page-capture.js", "x-bridge.js",
         "local-bridge.js", "popup.html", "LICENSE", "NOTICE.md")


def package():
    source = PROJECT / "browser-extension/space-capture"
    target = PROJECT / "static/space-capture.zip"
    for name in FILES:
        if not (source / name).is_file():
            raise RuntimeError("Browser component is incomplete")
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(source / name, name)
    print(target)


if __name__ == "__main__":
    package()
