"""The two host entry points: dashboard and provider login."""
from __future__ import annotations

import argparse
import sys

from .config import Config, ConfigError
from .docker import DockerError
from .manager import Manager
from .resources import ResourceError
from .tui import clean, run as run_dashboard


def parser():
    root = argparse.ArgumentParser(
        prog="acf",
        description="Agent Containment Facility — run without arguments to open the dashboard.",
        epilog="Manage environments, settings, services and files in the dashboard or container shell.",
    )
    sub = root.add_subparsers(dest="command")
    login = sub.add_parser("login", help="Sign in to an agent endpoint for use across projects")
    login.add_argument("endpoint", choices=("codex", "claude"))
    return root


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        config = Config()
        config.initialize()
        manager = Manager(config)
        manager.docker.ensure()
        if arguments.command == "login":
            manager.login(arguments.endpoint)
            print(f"{arguments.endpoint}: credentials saved. Open ./acf to select or create an environment.")
            return 0
        return run_dashboard(manager)
    except KeyboardInterrupt:
        print("\nInterrupted. Project data is preserved.", file=sys.stderr)
        return 130
    except (ConfigError, ResourceError, DockerError, ValueError, OSError) as exc:
        print("acf: " + clean(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
