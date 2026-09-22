import unittest

from detectors import redis_capacity as rc
from detectors.tests.helpers import fixture, pricing


class RedisCapacityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.info = fixture("redis_info.json")
        self.pricing = pricing()
        self.plan = rc.analyze(self.info, self.pricing)

    def test_growth_detected(self) -> None:
        self.assertGreater(self.plan.growth_gb_per_day, 0)
        self.assertIsNotNone(self.plan.days_to_maxmemory)
        self.assertLess(self.plan.days_to_maxmemory, 60)

    def test_recommended_size_covers_projection(self) -> None:
        self.assertGreaterEqual(self.plan.recommended_gb, self.plan.projected_90d_gb * 1.25)
        self.assertIn(self.plan.recommended_gb, rc.MEMORYSTORE_SIZE_STEPS_GB)

    def test_noeviction_with_partial_ttl_flagged(self) -> None:
        self.assertTrue(any("noeviction" in a for a in self.plan.actions))
        self.assertIn(self.plan.recommended_policy, {"allkeys-lru", "volatile-lru", "allkeys-lfu"})

    def test_volatile_lru_when_ttl_everywhere(self) -> None:
        info = dict(self.info, ttl_coverage_ratio=0.95)
        self.assertEqual(rc.analyze(info, self.pricing).recommended_policy, "volatile-lru")

    def test_shard_count_scales_with_ops(self) -> None:
        info = dict(self.info, ops_per_sec_p95=120_000)
        self.assertGreaterEqual(rc.analyze(info, self.pricing).recommended_shards, 5)

    def test_big_key_flagged(self) -> None:
        self.assertTrue(any("Big key" in a for a in self.plan.actions))

    def test_flat_series_has_no_deadline(self) -> None:
        info = dict(self.info)
        info["daily_samples"] = [dict(s, used_memory=1_000_000_000) for s in self.info["daily_samples"]]
        plan = rc.analyze(info, self.pricing)
        self.assertIsNone(plan.days_to_maxmemory)

    def test_markdown_render(self) -> None:
        md = rc.render_markdown(self.info, self.plan, "fixture")
        self.assertIn("maxmemory-policy", md)
        self.assertIn("## Memory trend", md)


if __name__ == "__main__":
    unittest.main()
