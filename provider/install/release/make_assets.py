#!/usr/bin/env python3
"""Build the release assets (used by .github/workflows/release.yml, also runnable by hand):

  python3 provider/install/release/make_assets.py --image ghcr.io/<owner>/<repo> [--version 1.2.0] [--out dist]

  dist/hama-provider-docker-<version>.zip   compose.yml + .env.example (HAMA_IMAGE pre-filled) + INSTALL.md: all you need for 'docker pull'
  dist/hama-provider-<version>.zip          the provider itself, for installs without Docker (Linux/Windows/any Python 3.8+)
  dist/SHA256SUMS
"""
import argparse
import hashlib
import os
import re
import sys
import zipfile

PROVIDER = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DOCKER = os.path.join(PROVIDER, "install", "docker")
SOURCE_ITEMS = ["hama_provider", "run.py", "config.example.json", "README.md", "MIGRATING.md",
                os.path.join("install", "linux"), os.path.join("install", "windows")]
SKIP = re.compile(r"(__pycache__|\.pyc$|\.DS_Store$)")

INSTALL_MD = """# HAMA provider {version}: Docker install

1. Unzip this folder anywhere on your Docker host and open a terminal in it.
2. `cp .env.example .env`, then edit `.env`: `MEDIA_DIR`, `PUID`/`PGID` (owner of your media), optional `HAMA_PLEX_URL`/`HAMA_PLEX_TOKEN`.
   `HAMA_IMAGE` is already set to `{image}:{version}` (use `:latest` to always get the newest release).
3. `mkdir -p data && sudo chown <PUID>:<PGID> data`
4. `docker compose pull && docker compose up -d`   (Docker Compose 2.24+)
5. Open `http://<docker-host>:32550/`, then add `http://<docker-host>:32550/tv` (and `/tvdb`, `/movies`) in Plex:
   Settings > Metadata Agents > Add Provider. Full guide: README.md in the repository.

Update: `docker compose pull && docker compose up -d`. Keep `.env` private (it may hold your Plex token).
"""


def version_from_source() -> str:
    with open(os.path.join(PROVIDER, "hama_provider", "__init__.py"), encoding="utf-8") as fh:
        return re.search(r'__version__\s*=\s*"([^"]+)"', fh.read()).group(1)


def add_tree(zf: zipfile.ZipFile, path: str, arc_root: str):
    if os.path.isfile(path):
        zf.write(path, os.path.join(arc_root, os.path.relpath(path, PROVIDER)))
        return
    for folder, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if not SKIP.search(d))
        for name in sorted(files):
            full = os.path.join(folder, name)
            if not SKIP.search(full):
                zf.write(full, os.path.join(arc_root, os.path.relpath(full, PROVIDER)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", required=True, help="image name without tag, e.g. ghcr.io/owner/repo")
    parser.add_argument("--version", default="", help="defaults to hama_provider.__version__")
    parser.add_argument("--out", default="dist")
    args = parser.parse_args(argv)
    version = (args.version or version_from_source()).lstrip("v")
    if version != version_from_source():
        sys.exit("version %s doesn't match hama_provider.__version__ %s" % (version, version_from_source()))
    image = args.image.lower()
    os.makedirs(args.out, exist_ok=True)

    docker_zip = os.path.join(args.out, "hama-provider-docker-%s.zip" % version)
    with zipfile.ZipFile(docker_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        root = "hama-provider-docker"
        zf.write(os.path.join(DOCKER, "compose.yml"), root + "/compose.yml")
        with open(os.path.join(DOCKER, ".env.example"), encoding="utf-8") as fh:
            env = fh.read()
        env = re.sub(r"^#?\s*HAMA_IMAGE=.*$", "HAMA_IMAGE=%s:%s" % (image, version), env, count=1, flags=re.MULTILINE)
        zf.writestr(root + "/.env.example", env)
        zf.writestr(root + "/INSTALL.md", INSTALL_MD.format(version=version, image=image))

    source_zip = os.path.join(args.out, "hama-provider-%s.zip" % version)
    with zipfile.ZipFile(source_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in SOURCE_ITEMS:
            add_tree(zf, os.path.join(PROVIDER, item), "hama-provider")

    sums = []
    for path in (docker_zip, source_zip):
        with open(path, "rb") as fh:
            sums.append("%s  %s" % (hashlib.sha256(fh.read()).hexdigest(), os.path.basename(path)))
    with open(os.path.join(args.out, "SHA256SUMS"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(sums) + "\n")
    print("\n".join(sums))


if __name__ == "__main__":
    main()
