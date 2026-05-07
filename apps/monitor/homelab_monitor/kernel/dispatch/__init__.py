"""Alert dispatch framework.

The ``AlertDispatcher`` fans out alert events to a list of ``Channel``
implementations. Per-channel failures are isolated (logged + counted) and
NEVER raised upward, so a flaky channel cannot disturb other deliveries or
block the publishing path.

Spec A ships the in-process dashboard channel (writes to the SSE broker).
Email, push, and other channels arrive in later stages.
"""
