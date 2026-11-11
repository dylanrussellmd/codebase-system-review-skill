"""Step 68: the review page lists points in natural order and the issues list in code order (numeric)."""
import unittest

from dev.tests import fixtures


class NaturalOrder(unittest.TestCase):
    def test_points_natural_order(self):
        """UI-H2 before UI-H12 before UI-H100 (plain text order would give H100, H12, H2)."""
        with fixtures.Ledger() as L:
            rid = L.review("R-0001")
            for pid in ("UI-H100", "UI-H2", "UI-H12"):
                L.finding(rid, pid, severity="H", domain_code="ui")
            L.commit()
            with fixtures.ServerThread(L) as srv:
                data = srv.get("/api/review/R-0001").json()
        self.assertEqual([f["point_id"] for f in data["findings"]], ["UI-H2", "UI-H12", "UI-H100"])

    def test_issues_list_in_code_order(self):
        """/api/issues orders each severity by issue number, the order codes were given in."""
        with fixtures.Ledger() as L:
            rid = L.review("R-0001")
            for _ in range(3):
                L.issue(first_seen_review_id=rid, severity="M")
            L.commit()
            with fixtures.ServerThread(L) as srv:
                codes = [i["code"] for i in srv.get("/api/issues").json()["rows"]]
        self.assertEqual(codes, ["ISS-0001", "ISS-0002", "ISS-0003"])


if __name__ == "__main__":
    unittest.main()
