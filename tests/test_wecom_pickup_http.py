"""End-to-end acceptance checks for the synthetic pickup-fleet dashboard."""

from __future__ import annotations

import unittest

from scripts.verify_wecom_pickup import (
    CORE_HEADERS,
    _build_fixture,
    _xlsx_rows,
    run_http_verification,
)


class WecomPickupHttpAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = _build_fixture()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def test_real_http_views_context_roles_and_exports(self):
        result = run_http_verification(self.fixture)
        checks = result["checks"]
        self.assertEqual(checks["details_view"]["status"], 200)
        self.assertIn("有效价格", checks["details_view"]["display_statuses"])
        self.assertIn("待完善", checks["details_view"]["display_statuses"])
        self.assertEqual(checks["encoded_operator_header"]["created_by_name"], "合成审核员")
        self.assertEqual(checks["single_export"]["headers"], CORE_HEADERS)
        self.assertEqual(checks["single_export"]["data_rows"], 1)
        self.assertEqual(checks["evidence"]["supplier_role"], "supplier")
        self.assertEqual(checks["evidence"]["original_sender"], "张调度")
        self.assertEqual(checks["cross_page_export"]["export_data_rows"], 520)
        self.assertFalse(checks["source_isolation"]["contains_foreign"])

    def test_independent_xlsx_reader_reads_generated_sample(self):
        result = run_http_verification(self.fixture)
        with open(result["sample_xlsx"], "rb") as stream:
            rows = _xlsx_rows(stream.read())
        self.assertEqual(rows[0], CORE_HEADERS)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1][0] and rows[1][1])


if __name__ == "__main__":
    unittest.main()
