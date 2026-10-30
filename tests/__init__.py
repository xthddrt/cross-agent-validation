import pathlib
import tempfile

from cav import flow

# Fake agents finish at once; a 2-second poll would be most of the suite's time.
flow.POLL_SECONDS = 0.05
# The agents' blind folders go to a scratch place, not the real one under LOCALAPPDATA.
flow.BLIND_ROOT = pathlib.Path(tempfile.gettempdir()) / "cav-blind-tests"
