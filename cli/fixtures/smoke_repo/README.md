"""Fixture repo for the 'smoke' benchmark subset.

Deliberately tiny: one module with a real bug (mean returns sum, not
mean) + one failing test. run_task should fix it offline when a scripted
model is injected (tests do this via harness.deps.set_call_model).
"""
