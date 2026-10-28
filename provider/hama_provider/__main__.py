"""Run with: python3 -m hama_provider [--config path] [--host H] [--port P] [--data-dir D]"""
import argparse
import logging
import os
import sys

from . import __version__, config
from .plexsync import PlexSync
from .prepare import Preparer
from .server import serve
from .service import Service


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hama_provider", description="HAMA Plex custom metadata provider")
    parser.add_argument("--config", help="path to config.json")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--data-dir")
    parser.add_argument("--sync-collections", action="store_true", help="push HAMA collections into Plex once, then exit")
    parser.add_argument("--prepare", metavar="LIBRARY", help="plan the changes that make ASS-style folders work with Plex's scanner")
    parser.add_argument("--apply", action="store_true", help="with --prepare: carry the plan out (renames, moves, .plexmatch)")
    parser.add_argument("--undo", metavar="LIBRARY", help="reverse everything --prepare --apply did in that library")
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    for key in ("host", "port", "data_dir"):
        if getattr(args, key) is not None:
            cfg[key] = getattr(args, key)
    os.makedirs(cfg.data_dir, exist_ok=True)

    handlers = [logging.StreamHandler(sys.stdout)]
    if cfg.log_file:
        handlers.append(logging.FileHandler(cfg.log_file, encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, str(cfg.log_level).upper(), logging.INFO), handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if args.prepare or args.undo:  # keep the plan readable
        logging.getLogger().setLevel(max(logging.WARNING, logging.getLogger().level))
    log = logging.getLogger("hama")
    log.info("HAMA provider %s, config: %s, data: %s", __version__, cfg.get("_config_file", "defaults/env"), cfg.data_dir)
    if not os.access(cfg.data_dir, os.W_OK):
        log.error("Data folder %s is not writable by this user (uid %s); nothing can be cached. Docker: "
                  "create it first and run 'sudo chown %s %s' on the host (PUID/PGID in .env)",
                  cfg.data_dir, os.getuid() if hasattr(os, "getuid") else "?", "PUID:PGID", "<your data folder>")

    service = Service(cfg)
    if args.undo:
        print("Undid %d changes in %s" % (Preparer().undo(args.undo), args.undo))
        return
    if args.prepare:  # no server threads (pre-fetch, loaders) next to the running provider: just plan/apply
        preparer = Preparer(service)
        plan = preparer.plan(args.prepare)
        print(plan.text())
        if args.apply:
            print("\nApplied %d changes. Undo with: --undo %s" % (preparer.apply(plan), args.prepare))
        elif any(a.kind != "warn" for a in plan.actions):
            print("\nPreview only. Run again with --apply to make these changes.")
        return
    service.start()
    if args.sync_collections:
        if not cfg.plex_url:
            parser.error("set plex_url (and plex_token) in config.json or HAMA_PLEX_URL / HAMA_PLEX_TOKEN")
        PlexSync(service).run_once()
        return
    PlexSync(service).start_schedule()
    serve(service)


if __name__ == "__main__":
    main()
