#!/bin/bash
# register_battlestation_runner.sh — the battlestation is just the first device
# in the fleet. Kept as a named entry point; all logic lives in
# register_device_runner.sh (any device, any OS). Same interface as before:
# no args = canary only, "all" = every pre-grail ring, or explicit repo names.
exec "$(dirname "$0")/register_device_runner.sh" --device battlestation --os windows "$@"
