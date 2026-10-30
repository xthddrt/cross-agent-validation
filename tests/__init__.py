from cav.orchestrator import Orchestrator

# Fake agents finish at once; the 2-second monitor poll was most of the suite's time.
Orchestrator.POLL_SECONDS = 0.05
