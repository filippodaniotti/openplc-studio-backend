"""Regenerate the freely available complete source archive from the pinned clone."""
import argparse
import json
from pathlib import Path
import subprocess
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

PIN = "8c353f738aa88fa9dee0dc1aa7d18d2a06441cb5"
ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "plc_platform_backend" / "static" / "listening-client"


def package(upstream: Path) -> None:
    revision = subprocess.run(["git", "-C", str(upstream), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    if revision != PIN:
        raise ValueError(f"Expected pinned upstream {PIN}, got {revision}")
    tracked = subprocess.run(["git", "-C", str(upstream), "ls-files", "-z"], check=True, capture_output=True).stdout.decode().split("\0")
    manifest = {"upstream_commit": PIN, "modified_on": "2026-10-16", "version": "Third-Party Modified Version of the webMUSHRA Software", "source_archive": "source.zip"}
    (ASSETS / "upstream.json").write_text(json.dumps(manifest, indent=2))
    with ZipFile(ASSETS / "source.zip", "w", compression=ZIP_DEFLATED) as archive:
        def add(name: str, path: Path):
            info = ZipInfo(name, date_time=(2026, 10, 16, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
        for relative in sorted(filter(None, tracked)):
            add("upstream-original/" + relative, upstream / relative)
        for path in sorted(ASSETS.rglob("*")):
            if path.is_file() and path.name != "source.zip":
                add("modified/" + path.relative_to(ASSETS).as_posix(), path)
        add("package_listening_client_source.py", Path(__file__))
        add("angular-host/global-styles.scss", ROOT.parent / "plc-platform-frontend" / "src" / "styles.scss")
        for path in sorted((ROOT.parent / "plc-platform-frontend" / "src" / "app" / "listening-studies").glob("*")):
            if path.is_file():
                add("angular-host/" + path.name, path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, default=ROOT.parent / "webMUSHRA")
    package(parser.parse_args().upstream)
