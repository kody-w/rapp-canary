"""A bounded echo tool for live provider acceptance; writes only the configured probe trace."""

import json
import os
from pathlib import Path

from agents.basic_agent import BasicAgent


class AstraProbeEcho(BasicAgent):
    def __init__(self):
        super().__init__(name="AstraProbeEcho", metadata={
            "name": "AstraProbeEcho",
            "description": "Echo the supplied value exactly. Use this tool when asked to perform the probe.",
            "parameters": {
                "type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"],
            },
        })

    def perform(self, value="", **kwargs):
        with Path(os.environ["ASTRA_PROBE_TRACE"]).open("a", encoding="utf-8") as trace:
            trace.write(json.dumps({"value": value}) + "\n")
        return value
