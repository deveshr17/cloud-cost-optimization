import unittest

import yaml

from detectors import k8s_rightsizing
from detectors.tests.helpers import fixture, pricing


class RightsizingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = fixture("prometheus_usage.json")
        self.vpa = fixture("vpa_recommendations.json")
        self.pricing = pricing()
        self.recs = k8s_rightsizing.analyze(self.data, self.pricing, self.vpa)

    def rec(self, workload: str, container: str):
        return next(r for r in self.recs if r.workload == workload and r.container == container)

    def test_over_provisioned_uses_p95_plus_headroom(self) -> None:
        r = self.rec("checkout-api", "app")
        self.assertEqual(r.status, "over")
        self.assertEqual(r.rec_cpu_request_m, 300)  # 210 * 1.3 = 273 -> 300
        self.assertEqual(r.rec_mem_request_mi, 832)  # 680 * 1.2 = 816 -> 832
        self.assertLess(r.monthly_delta_estimate, 0)

    def test_oom_and_throttling_flags_under(self) -> None:
        r = self.rec("ledger-worker", "worker")
        self.assertEqual(r.status, "under")
        self.assertGreater(r.rec_mem_request_mi, r.current_mem_request_mi)
        self.assertTrue(any("OOMKills" in n for n in r.notes))

    def test_within_tolerance_unchanged(self) -> None:
        r = self.rec("storefront", "istio-proxy")
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.rec_cpu_request_m, r.current_cpu_request_m)

    def test_cpu_limits_policy(self) -> None:
        none = k8s_rightsizing.analyze(self.data, self.pricing, cpu_limits="none")
        self.assertTrue(all(r.rec_cpu_limit_m is None for r in none))
        twice = k8s_rightsizing.analyze(self.data, self.pricing, cpu_limits="2x")
        r = next(x for x in twice if x.workload == "checkout-api")
        self.assertGreaterEqual(r.rec_cpu_limit_m, r.rec_cpu_request_m * 2)

    def test_memory_limit_equals_request(self) -> None:
        for r in self.recs:
            if r.status != "ok":
                self.assertEqual(r.rec_mem_limit_mi, r.rec_mem_request_mi)

    def test_vpa_comparison_attached(self) -> None:
        r = self.rec("checkout-api", "app")
        self.assertEqual(r.vpa_target_cpu_m, 260)
        self.assertIsNone(self.rec("otel-collector", "collector").vpa_target_cpu_m)

    def test_patch_is_valid_multi_doc_yaml(self) -> None:
        patch = k8s_rightsizing.render_kustomize_patch(self.recs)
        docs = [d for d in yaml.safe_load_all(patch) if d]
        kinds = {d["kind"] for d in docs}
        self.assertIn("CronJob", kinds)
        cron = next(d for d in docs if d["kind"] == "CronJob")
        self.assertIn("jobTemplate", cron["spec"])
        self.assertFalse(any(d["metadata"]["name"] == "storefront" and c["name"] == "istio-proxy" for d in docs for c in _containers(d)))

    def test_markdown_has_summary(self) -> None:
        md = k8s_rightsizing.render_markdown(self.data, self.recs, "fixture", "none")
        self.assertIn("Containers analysed", md)
        self.assertIn("VPA target", md)


def _containers(doc):
    spec = doc["spec"]
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]["spec"]["containers"]


if __name__ == "__main__":
    unittest.main()
