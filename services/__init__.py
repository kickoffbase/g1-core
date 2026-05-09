"""
Pluggable services. Each new feature (MQTT bridge, scheduler, button input,
…) is one file in this folder + one line in main.py to register it.
Adding a service must NEVER require touching `app/`.
"""
