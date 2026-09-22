import unittest

from detectors import storage_optimizer as so
from detectors.tests.helpers import fixture, pricing


class StorageOptimizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = fixture("gcs_buckets.json")
        self.pricing = pricing()
        self.plans, self.findings = so.analyze(self.data, self.pricing)

    def plan(self, name: str):
        return next(p for p in self.plans if p.bucket.endswith(name))

    def test_hot_bucket_not_downgraded(self) -> None:
        p = self.plan("static-assets")
        self.assertEqual(p.rules, [])
        self.assertEqual(p.saving, 0.0)
        self.assertTrue(any("Hot serving" in n for n in p.notes))

    def test_cold_logs_get_full_ladder(self) -> None:
        p = self.plan("app-logs")
        classes = [r.storage_class for r in p.rules if r.action == "SetStorageClass"]
        self.assertEqual(classes, ["NEARLINE", "COLDLINE", "ARCHIVE"][: len(classes)])
        self.assertGreater(p.saving, 0)

    def test_nearline_bucket_never_gets_warmer(self) -> None:
        p = self.plan("db-backups")
        self.assertTrue(all(r.storage_class in ("COLDLINE", "ARCHIVE") for r in p.rules if r.action == "SetStorageClass"))

    def test_versioned_bucket_gets_noncurrent_rules(self) -> None:
        p = self.plan("tmp-exports")
        deletes = [r for r in p.rules if r.action == "Delete"]
        self.assertEqual(len(deletes), 2)
        self.assertTrue(any("daysSinceNoncurrentTime" in r.condition for r in deletes))
        self.assertGreater(p.proposed_monthly, 0.0)

    def test_rules_serialise_to_gcs_shape(self) -> None:
        rule = so.LifecycleRule("SetStorageClass", {"age": 30, "matchesStorageClass": ["STANDARD"]}, "NEARLINE").to_gcs()
        self.assertEqual(rule["action"], {"type": "SetStorageClass", "storageClass": "NEARLINE"})

    def test_findings_sorted_and_labelled(self) -> None:
        self.assertEqual(self.findings[0].severity, "high")
        self.assertTrue(any("Missing required labels" in n for n in self.plan("tmp-exports").notes))

    def test_markdown_has_lifecycle_json(self) -> None:
        md = so.render_markdown(self.data, self.plans, "fixture", self.pricing)
        self.assertIn('"lifecycle"', md)
        self.assertIn("gcloud storage buckets update", md)


if __name__ == "__main__":
    unittest.main()
