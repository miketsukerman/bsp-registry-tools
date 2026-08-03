"""
Module runner shim so the GUI can invoke ``python -m bsp.cli_runner`` as a
subprocess.  This is equivalent to calling the ``bsp`` entry-point script but
works without relying on the script being on PATH inside the spawned process.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
