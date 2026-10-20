"""Exercise native input buttons through real browser observations and actions."""

import unittest

from benchmarks.support import fixture_server
from dompilot.actions import AgentDecision, build_action_space
from dompilot.browser import BrowserSession


class InputButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = fixture_server()
        cls.base_url = cls.server.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__(None, None, None)

    def setUp(self):
        self.browser = BrowserSession(headless=True).__enter__()
        self.addCleanup(self.browser.close)
        self.browser.open(f"{self.base_url}form.html")

    def click(self, snapshot, target):
        self.assertIn(target.id, build_action_space(snapshot).targets.get("CLICK", []))
        result = self.browser.execute(
            AgentDecision.model_validate(
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "decision": {"action": "CLICK", "target": target.id, "reason": "test"},
                }
            )
        )
        self.assertTrue(result.success, result.error)
        self.assertTrue(result.executed)

    def test_submit_input_has_value_name_and_submits_the_form(self):
        self.browser.page.set_content(
            """<form onsubmit="event.preventDefault();
              document.querySelector('output').textContent = event.submitter.id">
              <input id="submit-input" type="submit" value="Save">
              <button id="submit-button">Save</button>
            </form><output></output>"""
        )

        snapshot = self.browser.observe()

        self.assertEqual(
            [(target.kind, target.name, target.actions) for target in snapshot.targets],
            [("button", "Save", ["CLICK"]), ("button", "Save", ["CLICK"])],
        )
        self.assertEqual(snapshot.targets[0].value, "Save")
        self.click(snapshot, snapshot.targets[0])
        self.assertEqual(self.browser.page.locator("output").text_content(), "submit-input")

    def test_button_input_has_value_name_and_runs_its_click_handler(self):
        self.browser.page.set_content(
            """<input type="BuTtOn" value="  Run   action  "
              onclick="document.querySelector('output').textContent = 'clicked'">
            <output></output>"""
        )

        snapshot = self.browser.observe()

        self.assertEqual(len(snapshot.targets), 1)
        self.assertEqual(snapshot.targets[0].kind, "button")
        self.assertEqual(snapshot.targets[0].name, "Run action")
        self.assertEqual(snapshot.targets[0].actions, ["CLICK"])
        self.click(snapshot, snapshot.targets[0])
        self.assertEqual(self.browser.page.locator("output").text_content(), "clicked")

    def test_reset_input_has_value_name_and_restores_the_initial_field_value(self):
        self.browser.page.set_content(
            """<form><label>Name <input id="name" value="initial"></label>
            <input type="reset" value="Start over"></form>"""
        )
        self.browser.page.locator("#name").fill("edited")

        snapshot = self.browser.observe()

        self.assertEqual(len(snapshot.targets), 2)
        reset = snapshot.targets[1]
        self.assertEqual(
            (reset.kind, reset.name, reset.actions), ("button", "Start over", ["CLICK"])
        )
        self.click(snapshot, reset)
        self.assertEqual(self.browser.page.locator("#name").input_value(), "initial")

    def test_explicit_labels_take_precedence_over_button_values(self):
        self.browser.page.set_content(
            """<span id="labelled">Referenced name</span>
            <input type="submit" value="value one" aria-labelledby="labelled"
              aria-label="ARIA name">
            <input type="button" value="value two" aria-label="ARIA name">
            <label>Label name<input type="reset" value="value three"></label>
            <input type="text" value="field contents" placeholder="Field name">"""
        )

        snapshot = self.browser.observe()

        self.assertEqual(
            [target.name for target in snapshot.targets],
            ["Referenced name", "ARIA name", "Label name", "Field name"],
        )

    def test_unavailable_buttons_and_unsupported_input_types_are_omitted(self):
        self.browser.page.set_content(
            """<input type="submit" value="Disabled" disabled>
            <input type="button" value="Hidden" hidden>
            <input type="reset" value="ARIA disabled" aria-disabled="true">
            <input type="file"><input type="image" alt="Image">
            <input type="button" value="Available">"""
        )

        snapshot = self.browser.observe()

        self.assertEqual([target.name for target in snapshot.targets], ["Available"])
        self.assertEqual(build_action_space(snapshot).targets, {"CLICK": [1]})

    def test_changed_button_value_rejects_the_observed_click(self):
        self.browser.page.set_content(
            """<input type="button" value="Original"
              onclick="document.querySelector('output').textContent = 'clicked'">
            <output></output>"""
        )
        snapshot = self.browser.observe()
        self.assertEqual(len(snapshot.targets), 1)
        self.browser.page.locator("input").evaluate("node => node.value = 'Changed'")

        result = self.browser.execute(
            AgentDecision.model_validate(
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "decision": {"action": "CLICK", "target": 1, "reason": "test"},
                }
            )
        )

        self.assertFalse(result.success)
        self.assertFalse(result.executed)
        self.assertEqual(result.error, "stale_target")
        self.assertEqual(self.browser.page.locator("output").text_content(), "")


if __name__ == "__main__":
    unittest.main()
