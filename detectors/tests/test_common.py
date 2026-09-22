import unittest

from detectors import common


class CommonTests(unittest.TestCase):
    def test_percentile_nearest_rank(self) -> None:
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertEqual(common.percentile(values, 50), 5)
        self.assertEqual(common.percentile(values, 95), 10)
        self.assertEqual(common.percentile([], 95), 0.0)

    def test_linear_trend_positive_and_flat(self) -> None:
        self.assertAlmostEqual(common.linear_trend([1, 2, 3, 4]), 1.0)
        self.assertAlmostEqual(common.linear_trend([5, 5, 5]), 0.0)
        self.assertEqual(common.linear_trend([1]), 0.0)

    def test_rounding_helpers(self) -> None:
        self.assertEqual(common.round_up_cpu_millicores(101), 150)
        self.assertEqual(common.round_up_cpu_millicores(0), 50)
        self.assertEqual(common.round_up_mebibytes(33), 64)

    def test_md_table_and_money(self) -> None:
        table = common.md_table(["a", "b"], [[1, 2]])
        self.assertIn("| a | b |", table)
        self.assertIn("| 1 | 2 |", table)
        self.assertEqual(common.md_table(["a"], []), "_No rows._\n")
        self.assertEqual(common.fmt_money(1234.5), "1,234.50 USD")
        self.assertEqual(common.fmt_bytes(1024**3), "1.0 GiB")

    def test_age_days_uses_fixture_clock(self) -> None:
        now = common.parse_ts("2026-09-21T00:00:00Z")
        self.assertEqual(common.age_days("2026-09-01T00:00:00Z", now), 20)

    def test_sort_findings_orders_by_severity_then_cost(self) -> None:
        f = [
            common.Finding("c", "low-cheap", "low", 1, ""),
            common.Finding("c", "high-cheap", "high", 1, ""),
            common.Finding("c", "high-expensive", "high", 100, ""),
        ]
        ordered = [x.resource for x in common.sort_findings(f)]
        self.assertEqual(ordered, ["high-expensive", "high-cheap", "low-cheap"])


if __name__ == "__main__":
    unittest.main()
