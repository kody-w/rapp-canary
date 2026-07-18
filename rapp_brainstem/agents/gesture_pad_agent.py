import json

from agents.basic_agent import BasicAgent


class GesturePadAgent(BasicAgent):
    """The |||PAD||| sense — lets the model drive the gesture pad UI.

    The pad (served at /pad) renders up to 8 glowing option keys the user can
    pinch, click, or stare at to select. The model presents choices either by
    calling this tool or by appending the marker directly; the pad parses the
    marker out of the reply, shows the options, speaks the prompt, and sends
    the chosen value back through /chat — a hands-free back-and-forth.
    """

    def __init__(self):
        self.name = 'PresentPadOptions'
        self.metadata = {
            "name": self.name,
            "description": (
                "Present the user with tappable choices on the gesture pad UI. "
                "Call this whenever a small set of options would move the "
                "conversation forward faster than free text — next actions, "
                "confirmations, menu picks. Keep labels under 4 words and "
                "offer 2-8 options."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "One short spoken sentence inviting the choice."
                    },
                    "options": {
                        "type": "array",
                        "description": "2-8 choices to render as pad keys.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": "Key label, under 4 words."
                                },
                                "value": {
                                    "type": "string",
                                    "description": "The message sent back to you when this key is selected."
                                }
                            },
                            "required": ["label", "value"]
                        }
                    }
                },
                "required": ["prompt", "options"]
            }
        }
        super().__init__(name=self.name, metadata=self.metadata)

    def perform(self, **kwargs):
        prompt = kwargs.get("prompt", "Pick one.")
        options = kwargs.get("options", [])[:8]
        payload = json.dumps({"prompt": prompt, "options": options})
        return (
            "Pad options staged. End your response with this exact marker so "
            "the gesture pad renders them (after any |||VOICE||| section):\n"
            f"|||PAD|||{payload}|||"
        )

    def system_context(self):
        return (
            "GESTURE PAD SENSE: the user may be on a hands-free gesture UI "
            "that renders tappable option keys. When offering a small set of "
            "choices (next steps, confirmations, menus), append to the very "
            "end of your response: |||PAD|||{\"prompt\": \"<one short spoken "
            "sentence>\", \"options\": [{\"label\": \"<under 4 words>\", "
            "\"value\": \"<message sent back when selected>\"}]}||| with 2-8 "
            "options. The marker is invisible to the user; never mention it. "
            "If a |||VOICE||| section is present, put the PAD marker after it."
        )
