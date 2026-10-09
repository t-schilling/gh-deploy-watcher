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

    now = datetime.now(timezone.utc)
    try:
        config = load_config()
    except ConfigError as exc:
        from gh_deploy_watcher.config import Config
        from gh_deploy_watcher.state import State
        sys.stdout.write(render_menu(Config([]), State(polling=True), str(exc), now, SCRIPT))
        return
    sys.stdout.write(render_menu(config, load_state(), None, now, SCRIPT))


if __name__ == "__main__":
    try:
        from gh_deploy_watcher import actions
    except ImportError:
        main_fallback()
    else:
        actions.main(sys.argv[1:])
