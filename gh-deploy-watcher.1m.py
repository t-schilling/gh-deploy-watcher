#!/usr/bin/env python3
"""SwiftBar plugin entrypoint: symlinked into SwiftBar's plugin folder."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import gh_deploy_watcher.actions as actions  # noqa: E402

if __name__ == "__main__":
    sys.exit(actions.main(sys.argv[1:], script_path=os.path.abspath(__file__)))
