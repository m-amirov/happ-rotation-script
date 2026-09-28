import json
import random
import tempfile
import unittest
from pathlib import Path

from happ_rotation import (
    RotationConfig,
    RotationError,
    choose_next_server,
    load_config,
    load_state,
    save_state,
)


class RotationTests(unittest.TestCase):
    def test_round_robin_starts_with_first(self):
        servers = ["Германия", "Финляндия", "США"]
        self.assertEqual(
            choose_next_server(servers, None, "round-robin"), "Германия"
        )

    def test_round_robin_advances_and_wraps(self):
        servers = ["Германия", "Финляндия", "США"]
        self.assertEqual(
            choose_next_server(servers, "Германия", "round-robin"), "Финляндия"
        )
        self.assertEqual(
            choose_next_server(servers, "США", "round-robin"), "Германия"
        )

    def test_random_does_not_repeat_last_when_possible(self):
        servers = ["Германия", "Финляндия", "США"]
        rng = random.Random(1)
        for _ in range(20):
            self.assertNotEqual(
                choose_next_server(servers, "США", "random", rng), "США"
            )


class ConfigTests(unittest.TestCase):
    def test_config_valid_and_default_rows(self):
        cfg = RotationConfig.from_dict(
            {
                "subscription": "LagomVPN",
                "servers": ["Германия", "Финляндия", "США"],
                "interval_seconds": 60,
            }
        )
        self.assertEqual(cfg.row_for_server("Германия"), 0)
        self.assertEqual(cfg.row_for_server("США"), 2)

    def test_explicit_server_rows(self):
        cfg = RotationConfig.from_dict(
            {
                "subscription": "LagomVPN",
                "servers": ["Германия", "США"],
                "server_rows": {"Германия": 0, "США": 6},
                "interval_seconds": 60,
            }
        )
        self.assertEqual(cfg.row_for_server("США"), 6)

    def test_requires_two_servers(self):
        with self.assertRaises(RotationError):
            RotationConfig.from_dict(
                {
                    "subscription": "LagomVPN",
                    "servers": ["США"],
                    "interval_seconds": 60,
                }
            )

    def test_invalid_coordinate_ratio(self):
        with self.assertRaises(RotationError):
            RotationConfig.from_dict(
                {
                    "subscription": "LagomVPN",
                    "servers": ["Германия", "США"],
                    "interval_seconds": 60,
                    "click_x_ratio": 1.5,
                }
            )

    def test_load_config_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            state_path = Path(tmp) / "state.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "subscription": "LagomVPN",
                        "servers": ["Германия", "США"],
                        "mode": "round-robin",
                        "interval_seconds": 60,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            cfg = load_config(cfg_path)
            self.assertEqual(cfg.servers[1], "США")

            save_state(state_path, "США")
            state = load_state(state_path)
            self.assertEqual(state["last_server"], "США")


if __name__ == "__main__":
    unittest.main()
