"""Run the installed YAMBL command with Python's module entry point."""

import sys

from yambl_compiler.application.cli import main

sys.exit(main())
