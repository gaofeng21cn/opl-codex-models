#!/usr/bin/env python3
"""Mock Codex CLI for offline demo / tests.

Emulates the portion of the real `codex` interface this tool uses:
  - `codex --version`
  - `codex debug models --bundled`  -> emits a `{"models": [...]}` JSON
  - `codex debug models`            -> reads `model_catalog_json` from
                                       `$CODEX_HOME/config.toml` and emits that
                                       catalog; a missing catalog file exits 1,
                                       mirroring the real CLI.

Marked clearly as a MOCK. Any test that uses this must be reported as a
*simulated* integration, never as a real Codex integration.

Settings via environment:
  MOCK_CODEX_VERSION  -> printed for --version (default "mock-codex 9.9.9")
  MOCK_BUNDLED_JSON   -> path to a file holding `{"models":[...]}` JSON
                          (default: ./bundled_models.json next to this file)
  MOCK_EXIT_CODE      -> if set to a non-zero int, exit(1) for bundled call
  MOCK_SLEEP_SECONDS  -> if set, sleep before answering (to test timeouts)

Exit codes follow the real CLI convention: 0 on success, non-zero on error.
"""

import json
import os
import signal
import sys
import time
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))


def _read_catalog_from_config():
    """Resolve `model_catalog_json` from $CODEX_HOME/config.toml and emit it.

    Mirrors the real Codex: the catalog path is read from the top-level
    `model_catalog_json` key. A missing CODEX_HOME / config / catalog file
    exits non-zero so the compatibility probe's negative control behaves the
    same against the mock as against the real CLI.
    """
    home = os.environ.get("CODEX_HOME")
    if not home:
        print("CODEX_HOME not set", file=sys.stderr)
        return 1
    config_path = os.path.join(home, "config.toml")
    if not os.path.isfile(config_path):
        print(f"config.toml not found: {config_path}", file=sys.stderr)
        return 1
    try:
        with open(config_path, "rb") as fh:
            parsed = tomllib.load(fh)
    except (OSError, ValueError) as exc:
        print(f"cannot parse config.toml: {exc}", file=sys.stderr)
        return 1
    catalog_path = parsed.get("model_catalog_json")
    if not catalog_path:
        print("model_catalog_json not set in config.toml", file=sys.stderr)
        return 1
    if not os.path.isfile(catalog_path):
        print(f"catalog not found: {catalog_path}", file=sys.stderr)
        return 1
    with open(catalog_path, "r", encoding="utf-8") as fh:
        sys.stdout.write(fh.read())
    return 0


def main(argv):
    if any(a in ("--version", "-v") for a in argv):
        print(os.environ.get("MOCK_CODEX_VERSION", "mock-codex 9.9.9"))
        return 0

    if "debug" in argv and "models" in argv:
        if "--bundled" in argv:
            exit_code = os.environ.get("MOCK_EXIT_CODE")
            if exit_code not in (None, "0", ""):
                print("simulated failure: MOCK_EXIT_CODE set", file=sys.stderr)
                return int(exit_code)
            sleep = os.environ.get("MOCK_SLEEP_SECONDS")
            if sleep:
                time.sleep(float(sleep))
            bundled_file = os.environ.get("MOCK_BUNDLED_JSON")
            if not bundled_file:
                bundled_file = os.path.join(HERE, "bundled_models.json")
            with open(bundled_file, "r", encoding="utf-8") as fh:
                sys.stdout.write(fh.read())
            return 0
        return _read_catalog_from_config()

    print(f"mock codex: unsupported invocation {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))