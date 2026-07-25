import unittest

import prolific_monitor as monitor


class RewardFormattingTests(unittest.TestCase):
    def test_formats_raw_prolific_reward_in_pounds(self) -> None:
        self.assertEqual(
            monitor.format_prolific_reward({"reward": 398.0}),
            "\N{POUND SIGN}3.98",
        )

    def test_uses_currency_from_nested_reward(self) -> None:
        self.assertEqual(
            monitor.format_prolific_reward(
                {"reward": {"amount": 250, "currency": "USD"}}
            ),
            "$2.50",
        )

    def test_formatted_study_matches_prolific_display(self) -> None:
        message = monitor.format_study(
            {"id": "test", "title": "Test study", "reward": 398.0}
        )
        self.assertIn("Reward: \N{POUND SIGN}3.98", message)


if __name__ == "__main__":
    unittest.main()
