"""Authoritative quality-control pass (ticket #7).

Runs as a one-shot Kubernetes Job per uploaded master. Judges the RECORD —
surface noise, clicks, channel health, loops — where the laptop's integrity
gate judged the capture chain. Verdicts: pass / review / recapture.
"""
