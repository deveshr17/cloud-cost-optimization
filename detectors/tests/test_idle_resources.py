import unittest

from detectors import idle_resources
from detectors.tests.helpers import fixture, pricing


class IdleResourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inv = fixture("idle_resources.json")
        self.pricing = pricing()
        self.findings = idle_resources.analyze(self.inv, self.pricing)

    def by_cat(self, cat: str):
        return [f for f in self.findings if f.category == cat]

    def test_unattached_disks_detected_and_attached_ignored(self) -> None:
        names = {f.resource.rsplit("/", 1)[-1] for f in self.by_cat("unattached-disk")}
        self.assertEqual(names, {"pd-orphan-postgres-data", "pd-ci-runner-scratch", "pd-legacy-jenkins-home"})

    def test_keep_label_marks_excluded(self) -> None:
        excluded = [f for f in self.findings if f.details.get("excluded")]
        self.assertTrue(any("pd-ci-runner-scratch" in f.resource for f in excluded))
        self.assertTrue(all("keep=true" in f.recommendation for f in excluded))

    def test_disk_cost_uses_type_price(self) -> None:
        disk = next(f for f in self.findings if "pd-orphan-postgres-data" in f.resource)
        self.assertAlmostEqual(disk.monthly_cost_estimate, 500 * self.pricing["compute"]["pd_ssd_gb_month"], places=2)

    def test_idle_ip_only_when_reserved_and_unused(self) -> None:
        ips = {f.resource.rsplit("/", 1)[-1] for f in self.by_cat("idle-static-ip")}
        self.assertEqual(ips, {"ip-old-lb-frontend", "ip-nat-standby"})

    def test_stopped_vm_respects_min_days(self) -> None:
        vms = {f.resource.rsplit("/", 1)[-1] for f in self.by_cat("stopped-vm-with-disks")}
        self.assertEqual(vms, {"batch-worker-legacy"})  # uat-bastion stopped 11d < 14d
        strict = idle_resources.analyze(self.inv, self.pricing, min_stopped_days=5)
        self.assertIn("uat-bastion", {f.resource.rsplit("/", 1)[-1] for f in strict if f.category == "stopped-vm-with-disks"})

    def test_old_snapshot_threshold(self) -> None:
        snaps = {f.resource.rsplit("/", 1)[-1] for f in self.by_cat("old-snapshot")}
        self.assertNotIn("snap-api-gateway-weekly", snaps)
        self.assertIn("snap-jenkins-2024-12-24", snaps)

    def test_empty_node_pool_and_lb(self) -> None:
        self.assertEqual([f.resource for f in self.by_cat("empty-node-pool")], ["gke-dev-primary/nodePools/ml-experiments"])
        self.assertEqual(len(self.by_cat("lb-no-backends")), 1)

    def test_cloudsql_unused_by_connections(self) -> None:
        dbs = {f.resource.rsplit("/", 1)[-1] for f in self.by_cat("unused-cloudsql")}
        self.assertEqual(dbs, {"mysql-legacy-cms", "pg-analytics-replica"})

    def test_markdown_render_contains_sections(self) -> None:
        md = idle_resources.render_markdown(self.inv, self.findings, "fixture")
        for heading in ("## Summary", "## Actionable findings", "## Excluded", "keep=true"):
            self.assertIn(heading, md)
        self.assertIn("not realised savings", md)

    def test_cli_offline_writes_outputs(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            md, js = Path(tmp) / "r.md", Path(tmp) / "r.json"
            rc = idle_resources.main(["--from-json", str(__import__("detectors.tests.helpers", fromlist=["FIXTURES"]).FIXTURES / "idle_resources.json"), "--md-out", str(md), "--json-out", str(js)])
            self.assertEqual(rc, 0)
            self.assertTrue(md.exists() and js.exists())


if __name__ == "__main__":
    unittest.main()
