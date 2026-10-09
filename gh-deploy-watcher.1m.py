#!/usr/bin/env python3
"""SwiftBar plugin entrypoint: symlinked into SwiftBar's plugin folder."""
import os
import sys

SCRIPT = os.path.realpath(__file__)
sys.path.insert(0, os.path.dirname(SCRIPT))


def main_fallback() -> None:
    """Render from saved state until the actions module exists."""
    from datetime import datetime, timezone

    from gh_deploy_watcher.config import ConfigError, load_config
    from gh_deploy_watcher.render import render_menu
    from gh_deploy_watcher.state import load_state

    from gh_deploy_watcher.config import Config
    from gh_deploy_watcher.state import State

    now = datetime.now(timezone.utc)
    try:
        config = load_config()
        out = render_menu(config, load_state(), None, now, SCRIPT)
    except (ConfigError, ValueError) as exc:
        out = render_menu(Config([]), State(polling=True), str(exc), now, "/usr/bin/true")
    sys.stdout.write(out)


if __name__ == "__main__":
    try:
        import gh_deploy_watcher.actions as actions
    except ModuleNotFoundError as exc:
        if exc.name != "gh_deploy_watcher.actions":
            raise
        main_fallback()
    else:
        actions.main(sys.argv[1:])
