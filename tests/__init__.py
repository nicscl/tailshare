"""Tests never touch the real ~/.tailshare: anything that falls back to the default state dir gets a temp one."""

import os
import tempfile

os.environ["TAILSHARE_STATE"] = tempfile.mkdtemp(prefix="tailshare-tests-state-")
