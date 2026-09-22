import unittest

from detectors import bigquery_optimizer as bq
from detectors.tests.helpers import fixture, pricing


class BigQueryOptimizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = fixture("bigquery_jobs.json")
        self.pricing = pricing()
        self.findings, self.be = bq.analyze(self.data, self.pricing)

    def cat(self, name: str):
        return [f for f in self.findings if f.category == name]

    def test_on_demand_cost_per_tib(self) -> None:
        self.assertAlmostEqual(bq.on_demand_cost(bq.TiB, self.pricing), self.pricing["bigquery"]["on_demand_per_tib"])

    def test_top_queries_ranked_by_bytes(self) -> None:
        top = self.cat("expensive-query")
        self.assertEqual(top[0].resource, "q-7f3a")
        self.assertIn("SELECT *", top[0].recommendation)

    def test_unpartitioned_requires_temporal_column(self) -> None:
        unpart = {f.resource for f in self.cat("unpartitioned-table")}
        self.assertIn("events.raw_events", unpart)
        self.assertNotIn("dim.users", unpart)
        self.assertIn("dim.users", {f.resource for f in self.cat("unclustered-table")})

    def test_partition_expiration_for_stale_table(self) -> None:
        stale = {f.resource for f in self.cat("partition-expiration")}
        self.assertIn("staging.legacy_imports_2023", stale)

    def test_slot_breakeven_recommendation(self) -> None:
        self.assertGreater(self.be.total_slot_hours, 0)
        self.assertIn(self.be.recommendation.split(";")[0].split(" ")[0], {"Stay", "Move", "Near"})
        self.assertLess(self.be.on_demand_monthly, self.be.slots_standard_monthly)

    def test_slot_breakeven_flips_when_on_demand_expensive(self) -> None:
        heavy = dict(self.data)
        heavy["jobs"] = [dict(j, total_bytes_billed=j["total_bytes_billed"] * 10) for j in self.data["jobs"]]
        _, be = bq.analyze(heavy, self.pricing)
        self.assertTrue(be.recommendation.startswith("Move"))

    def test_markdown_contains_ddl(self) -> None:
        md = bq.render_markdown(self.data, self.findings, self.be, "fixture")
        self.assertIn("PARTITION BY DATE(event_ts)", md)
        self.assertIn("partition_expiration_days", md)


if __name__ == "__main__":
    unittest.main()
