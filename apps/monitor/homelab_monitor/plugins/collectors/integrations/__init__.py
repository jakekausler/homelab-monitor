"""Integration-bundle collectors.

Each integration is a subpackage exposing ``register_all(loader)``. The lifespan
imports each bundle and calls ``register_all`` so the bundle owns its own
per-collector registration + failure isolation. ``homeassistant`` is the
exemplar; EPICs 006/007/008/021 copy this layout.
"""
