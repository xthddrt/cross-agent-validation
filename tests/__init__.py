from cav import flow

# Fake agents finish at once; a 2-second poll would be most of the suite's time.
flow.POLL_SECONDS = 0.05
