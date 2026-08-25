import json
import unittest

from execute_approved import PARENT_NAME, plan


PARENT = [{"account_id": "PARENT1", "name": PARENT_NAME}]


class PlanTests(unittest.TestCase):
    def proposal(self, kind, changes, account="OLD1"):
        return {
            "proposal_type": kind, "crm_account_id": account,
            "website_name": "Bellhaven Test", "website_address": "1 Main St",
            "website_city": "Testville", "website_state": "OH", "website_zip": "43000",
            "proposed_changes": json.dumps(changes),
        }

    def test_reparent_uses_parent_id(self):
        operations = plan(self.proposal("REPARENT", {"parent_name": PARENT_NAME}), PARENT)
        self.assertEqual(operations[0].payload, {"parent_id": "PARENT1"})

    def test_duplicate_has_all_required_safety_fields(self):
        proposal = self.proposal("DUPLICATE", {
            "retain_account": "WIN", "review_duplicate_accounts": ["WIN", "LOSE"]
        })
        operation = plan(proposal, PARENT)[0]
        self.assertEqual(operation.payload["status"], "Inactive")
        self.assertEqual(operation.payload["duplicate_of_account"], "WIN")
        self.assertTrue(operation.payload["note"])

    def test_chow_old_account_update_contains_only_link(self):
        changes = {
            "create_new_account": {
                "name": "New", "parent_name": PARENT_NAME, "address": "1 Main",
                "city": "X", "state": "OH", "zip": "43000",
            },
            "set_on_old_account": {"chow_current_account": "<new_account_id>"},
            "do_not_reparent_old_account": True,
        }
        operations = plan(self.proposal("CHOW_CREATE_NEW", changes), PARENT)
        self.assertEqual(operations[1].payload, {"chow_current_account": "<new_account_id>"})
        self.assertNotIn("parent_id", operations[1].payload)

    def test_stale_review_marker_is_not_written(self):
        with self.assertRaisesRegex(ValueError, "no explicit status or note"):
            plan(self.proposal("STALE_UNDER_BELLHAVEN", {"review_account_status": "Active"}), PARENT)

    def test_no_action_never_writes(self):
        self.assertEqual(plan(self.proposal("NO_ACTION", {}), PARENT), [])

    def test_create_uses_api_billing_schema(self):
        operation = plan(self.proposal("CREATE_NEW", {}), PARENT)[0]
        self.assertEqual(operation.payload["parent_id"], "PARENT1")
        self.assertIn("billing_street", operation.payload)


if __name__ == "__main__":
    unittest.main()
