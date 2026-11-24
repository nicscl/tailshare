"""Tests never touch the real ~/.tailshare: anything that falls back to the default state dir gets a temp one."""

import os
import tempfile

os.environ["TAILSHARE_STATE"] = tempfile.mkdtemp(prefix="tailshare-tests-state-")

# The old start path is retired on real hosts (start.retired); its own tests still exercise it. Inherited by the
# gate subprocesses the tests run. tests/test_start.Retired checks the real default.
os.environ["TAILSHARE_LEGACY_START"] = "1"
