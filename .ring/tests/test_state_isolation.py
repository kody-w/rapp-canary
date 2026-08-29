import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def source(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read().replace("\r\n", "\n")


class RingProcessesKeepIndependentState(unittest.TestCase):
    def test_flights_use_their_own_state_directory(self):
        script = source(".ring/tools/flight.sh")
        self.assertIn('state="$base/state"', script)
        self.assertIn('BRAINSTEM_STATE_DIR="$state"', script)
        self.assertIn('"$HOME/.brainstem/state/$name_in_state"', script)

    def test_soak_uses_its_own_state_directory(self):
        script = source(".ring/tools/soak.sh")
        self.assertIn('state="$SOAK_HOME/state"', script)
        self.assertIn('BRAINSTEM_STATE_DIR="$state"', script)
        self.assertIn('"$HOME/.brainstem/state/.copilot_token"', script)


if __name__ == "__main__":
    unittest.main()
