"""The benchmark pages must provide stable, independently checkable outcomes."""

import json

from dompilot.actions import ModelReply, RunConfig
from dompilot.agent import Agent


def test_search_fixture(browser, site):
    browser.open(site + "search.html")
    page = browser.page
    page.get_by_role("searchbox", name="Search").fill("Transformer")
    page.get_by_role("button", name="Search").click()
    assert page.get_by_role("status").inner_text() == "Search results: Transformer"


def test_form_fixture(browser, site):
    browser.open(site + "form.html")
    page = browser.page
    page.get_by_role("textbox", name="Name").fill("Ada")
    page.get_by_role("checkbox", name="Accept").check()
    page.get_by_role("button", name="Submit").click()
    assert page.get_by_role("status").inner_text() == "Submitted: Ada; accepted"


def test_dropdown_fixture(browser, site):
    browser.open(site + "dropdown.html")
    page = browser.page
    page.get_by_label("Language").select_option("python")
    page.get_by_role("button", name="Apply").click()
    assert page.get_by_role("status").inner_text() == "Selected: python"


def test_dynamic_fixture(browser, site):
    browser.open(site + "dynamic.html")
    page = browser.page
    page.get_by_role("button", name="Reveal").click()
    page.get_by_role("textbox", name="Dynamic text").fill("hello")
    page.get_by_role("button", name="Save").click()
    assert page.get_by_role("status").inner_text() == "Saved: hello"


def test_disabled_fixture(browser, site):
    browser.open(site + "disabled.html")
    page = browser.page
    assert not page.locator("button[hidden]").is_visible()
    assert not page.locator("button.hidden-by-css").is_visible()
    assert page.get_by_role("button", name="Disabled native").is_disabled()
    assert page.get_by_role("button", name="Disabled ARIA").get_attribute("aria-disabled") == "true"
    assert page.get_by_label("Read only").get_attribute("readonly") is not None
    assert page.get_by_label("Disabled input").is_disabled()
    assert page.get_by_label("Enabled input").is_enabled()


def test_large_text_fixture(browser, site):
    browser.open(site + "large_text.html?words=15000")
    page = browser.page
    assert len(page.locator("#unrelated").inner_text().split()) == 15000
    page.get_by_role("searchbox", name="Search").fill("Transformer")
    page.get_by_role("button", name="Search").click()
    assert page.get_by_role("status").inner_text() == "Search results: Transformer"


def test_premature_done_fails_independent_verifier(site, tmp_path):
    class PrematureDone:
        def decide(self, input):
            return ModelReply(
                text=json.dumps(
                    {
                        "snapshot_id": input.snapshot.snapshot_id,
                        "decision": {"action": "DONE", "reason": "I claim it is done"},
                    }
                )
            )

    result = Agent(PrematureDone(), RunConfig(headless=True, run_dir=tmp_path)).run(
        site + "search.html",
        "Search for Transformer and submit the search.",
        lambda page: (
            "passed"
            if page.locator('[role="status"]').inner_text() == "Search results: Transformer"
            else "failed"
        ),
    )
    assert result.status == "verification_failed"
    assert result.verification == "failed"
