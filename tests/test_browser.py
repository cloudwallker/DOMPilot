"""Browser boundary tests use real Chromium pages and the local fixture site."""

import time

import pytest

from dompilot.actions import AgentDecision, build_action_space, compact_json
from dompilot.browser import BrowserSession


def decide(snapshot, action, **kwargs):
    return AgentDecision.model_validate(
        {
            "snapshot_id": snapshot.snapshot_id,
            "decision": {"action": action, "reason": "browser test", **kwargs},
        }
    )


def target(snapshot, name):
    return next(item for item in snapshot.targets if item.name.casefold() == name.casefold())


@pytest.mark.parametrize(
    "page,expected",
    [
        ("search", {"Search": "TYPE_TEXT"}),
        ("form", {"Name": "TYPE_TEXT", "Accept": "CLICK"}),
        ("dropdown", {"Language": "SELECT"}),
        ("dynamic", {"Reveal": "CLICK"}),
        ("disabled", {"Enabled input": "TYPE_TEXT", "Enabled action": "CLICK"}),
        ("large_text.html?words=20000", {"Search": "TYPE_TEXT"}),
    ],
)
def test_fixture_semantics(browser, site, page, expected):
    browser.open(site + page + ("" if ".html" in page else ".html"))
    snapshot = browser.observe()
    for name, action in expected.items():
        assert action in target(snapshot, name).actions
    if page == "disabled":
        assert {item.name for item in snapshot.targets}.isdisjoint(
            {
                "Hidden attribute",
                "Hidden CSS",
                "Disabled native",
                "Disabled ARIA",
                "Read only",
                "Disabled input",
            }
        )
    if page.startswith("large_text"):
        assert len(compact_json(snapshot.model_dump())) < 12000
        assert "granite tundra harbor" not in compact_json(snapshot.model_dump())


def test_real_actions_use_snapshot_targets(browser, site):
    browser.open(site + "form.html")
    first = browser.observe()
    result = browser.execute(
        decide(first, "TYPE_TEXT", target=target(first, "Name").id, text="Ada")
    )
    assert result.success and result.executed
    assert not browser.execute(
        decide(first, "TYPE_TEXT", target=target(first, "Name").id, text="wrong")
    ).success
    second = browser.observe()
    assert target(second, "Name").value == "Ada"
    assert browser.execute(decide(second, "CLICK", target=target(second, "Accept").id)).success
    third = browser.observe()
    assert target(third, "Accept").checked
    assert browser.execute(decide(third, "CLICK", target=target(third, "Submit").id)).success
    assert "Submitted: Ada; accepted" in browser.observe().feedback


def test_select_and_dynamic_page(browser, site):
    browser.open(site + "dropdown.html")
    snap = browser.observe()
    select = target(snap, "Language")
    assert {option.value for option in select.options} >= {"python", "javascript", "rust"}
    assert browser.execute(decide(snap, "SELECT", target=select.id, value="python")).success
    snap = browser.observe()
    assert target(snap, "Language").value == "python"
    assert browser.execute(decide(snap, "CLICK", target=target(snap, "Apply").id)).success
    assert "Selected: python" in browser.observe().feedback
    browser.open(site + "dynamic.html")
    snap = browser.observe()
    assert browser.execute(decide(snap, "CLICK", target=target(snap, "Reveal").id)).success
    browser.page.wait_for_selector("#dynamic-text")
    assert "Dynamic text" in {t.name for t in browser.observe().targets}


def test_reordering_keeps_original_node_reference(browser, site):
    browser.open(site + "search.html")
    browser.page.set_content(
        '<button id="a" onclick="window.hit=\'a\'">A</button>'
        '<button id="b" onclick="window.hit=\'b\'">B</button>'
    )
    snap = browser.observe()
    browser.page.evaluate("document.body.prepend(document.querySelector('#b'))")
    result = browser.execute(decide(snap, "CLICK", target=target(snap, "A").id))
    assert result.success
    assert browser.page.evaluate("window.hit") == "a"


@pytest.mark.parametrize(
    "change",
    [
        "document.querySelector('#a').replaceWith(document.createElement('button'))",
        "document.querySelector('#a').textContent='Changed'",
        "document.querySelector('#a').disabled=true",
        "document.querySelector('#a').setAttribute('aria-label','Changed')",
    ],
)
def test_changed_targets_are_stale(browser, site, change):
    browser.open(site + "search.html")
    browser.page.set_content("<button id='a' onclick='window.hit=true'>Original</button>")
    snap = browser.observe()
    browser.page.evaluate(change)
    result = browser.execute(decide(snap, "CLICK", target=target(snap, "Original").id))
    assert not result.success and not result.executed and result.error == "stale_target"
    assert browser.page.evaluate("window.hit === undefined")


def test_navigation_invalidates_snapshot(browser, site):
    browser.open(site + "form.html")
    snap = browser.observe()
    browser.open(site + "search.html")
    result = browser.execute(decide(snap, "CLICK", target=target(snap, "Submit").id))
    assert not result.success and result.error == "stale_target"


def test_changed_base_url_invalidates_relative_link(browser, site):
    browser.open(site + "search.html")
    browser.page.set_content(
        '<base href="/old/"><a href="result" '
        'onclick="event.preventDefault(); window.hit=this.href">Result</a>'
    )
    snap = browser.observe()
    browser.page.evaluate("document.querySelector('base').href='/new/'")
    result = browser.execute(decide(snap, "CLICK", target=target(snap, "Result").id))
    assert not result.success and not result.executed and result.error == "stale_target"
    assert browser.page.evaluate("window.hit === undefined")


def test_relative_link_safety_uses_document_base_url(browser, site):
    browser.open(site + "search.html")
    browser.page.set_content(
        '<base href="http://user:pass@localhost/">'
        '<a href="result">Credentialed relative link</a>'
        '<a href="https://example.com/result">Safe absolute link</a>'
    )
    snap = browser.observe()
    assert [item.name for item in snap.targets] == ["Safe absolute link"]


@pytest.mark.parametrize("action", ["WAIT", "SCROLL_DOWN"])
def test_external_navigation_invalidates_even_control_actions(browser, site, action):
    browser.open(site + "search.html")
    browser.page.evaluate("document.body.style.minHeight='2000px'")
    snap = browser.observe()
    browser.page.goto(site + "form.html")
    result = browser.execute(decide(snap, action))
    assert not result.success and not result.executed and result.error == "stale_target"


@pytest.mark.parametrize(
    "change",
    [
        "document.querySelector('#field').value='changed'",
        "document.querySelector('#field').setAttribute('aria-label','Label ' + 'z'.repeat(150))",
        "document.querySelector('#field').setAttribute('aria-readonly','true')",
    ],
)
def test_same_node_field_change_invalidates_snapshot(browser, site, change):
    browser.open(site + "search.html")
    browser.page.set_content(
        '<input id="field" aria-label="Label ' + "x" * 150 + '" value="initial">'
    )
    snap = browser.observe()
    browser.page.evaluate(change)
    result = browser.execute(decide(snap, "TYPE_TEXT", target=snap.targets[0].id, text="new"))
    assert not result.success and not result.executed and result.error == "stale_target"


def test_same_node_checkbox_and_select_option_changes_are_stale(browser, site):
    browser.open(site + "search.html")
    browser.page.set_content(
        '<label><input id="check" type="checkbox">Agree</label>'
        '<select id="pick" aria-label="Pick"><option value="a">A</option></select>'
    )
    snap = browser.observe()
    browser.page.evaluate("document.querySelector('#check').checked=true")
    assert browser.execute(decide(snap, "CLICK", target=target(snap, "Agree").id)).error == (
        "stale_target"
    )
    snap = browser.observe()
    browser.page.evaluate("document.querySelector('option').disabled=true")
    assert (
        browser.execute(decide(snap, "SELECT", target=target(snap, "Pick").id, value="a")).error
        == "stale_target"
    )


def test_input_value_preserves_whitespace(browser, site):
    browser.open(site + "search.html")
    browser.page.set_content('<input aria-label="Query" value="  Ada  ">')
    assert target(browser.observe(), "Query").value == "  Ada  "


def test_observation_is_bounded_and_keeps_ids(browser, site):
    browser.open(site + "search.html")
    browser.page.set_content(
        "<fieldset><legend>"
        + "L" * 200
        + "</legend>"
        + "".join(
            f"<button style='display:block;height:7px' placeholder='{'P' * 120}'>"
            f"{i:03d} {'X' * 200}</button>"
            for i in range(100)
        )
        + "</fieldset>"
    )
    snap = browser.observe()
    assert [t.id for t in snap.targets] == list(range(1, len(snap.targets) + 1))
    assert (
        len(
            compact_json(
                {
                    "snapshot": snap.model_dump(),
                    "action_space": build_action_space(snap).model_dump(),
                }
            )
        )
        <= 12000
    )
    assert len(snap.targets) < 50
    assert snap.omitted > 0


def test_challenge_detection_does_not_confuse_login_link(browser, site):
    browser.open(site + "search.html")
    browser.page.set_content("<a href='/login'>Log in</a><button>Search</button>")
    assert browser.observe().blocked_reason is None
    browser.page.set_content("<h1>Sign in</h1><input type='password' aria-label='Password'>")
    assert browser.observe().blocked_reason
    browser.page.set_content("<iframe title='Security challenge' src='about:blank'></iframe>")
    assert browser.observe().blocked_reason


def test_otp_and_automation_notice_block_without_closing_page(browser, site):
    browser.open(site + "search.html")
    browser.page.set_content("<h1>Enter verification code</h1><input autocomplete='one-time-code'>")
    assert browser.observe().blocked_reason
    assert browser.alive
    browser.open(site + "search.html")
    browser.page.set_content("<h1>Automated access is not allowed</h1>")
    assert browser.observe().blocked_reason
    assert browser.alive


def test_new_challenge_blocks_previously_observed_click(browser, site):
    browser.open(site + "search.html")
    browser.page.set_content("<button onclick='window.hit=true'>Submit</button>")
    snap = browser.observe()
    browser.page.evaluate("document.body.insertAdjacentHTML('beforeend', '<input type=password>')")
    assert browser.check_blocked() == "visible_password_form"
    result = browser.execute(decide(snap, "CLICK", target=target(snap, "Submit").id))
    assert not result.success and not result.executed
    assert browser.page.evaluate("window.hit === undefined")


def test_links_and_controls_outside_safety_boundary_are_hidden(browser, site):
    browser.open(site + "search.html")
    browser.page.set_content(
        '<a href="javascript:alert(1)">Script</a>'
        '<a href="mailto:a@example.com">Mail</a>'
        '<a href="https://example.com">Web</a>'
        '<input aria-label="Read only" aria-readonly="true">'
        '<button style="opacity:0">Transparent</button>'
        '<select aria-label="Pick"><optgroup label="Disabled" disabled>'
        '<option value="bad">Bad</option></optgroup><option value="good">Good</option></select>'
    )
    snap = browser.observe()
    assert {item.name for item in snap.targets} == {"Web", "Pick"}
    assert [option.value for option in target(snap, "Pick").options] == ["good"]


@pytest.mark.parametrize(
    "disabled_option",
    [
        '<option disabled value="x">Disabled</option>',
        '<optgroup disabled label="Disabled"><option value="x">Disabled</option></optgroup>',
    ],
)
def test_select_omits_value_when_first_matching_option_is_disabled(browser, site, disabled_option):
    browser.open(site + "search.html")
    browser.page.set_content(
        '<select aria-label="Pick"><option value="init">Initial</option>'
        + disabled_option
        + '<option value="x">Enabled duplicate</option><option value="safe">Safe</option></select>'
    )
    snap = browser.observe()
    pick = target(snap, "Pick")
    assert [option.value for option in pick.options] == ["init", "safe"]
    assert "x" not in build_action_space(snap).select_options[pick.id]
    result = browser.execute(decide(snap, "SELECT", target=pick.id, value="safe"))
    assert result.success and result.executed
    assert browser.page.locator("select").input_value() == "safe"


def test_select_keeps_value_when_first_matching_option_is_enabled(browser, site):
    browser.open(site + "search.html")
    browser.page.set_content(
        '<select aria-label="Pick"><option value="init">Initial</option>'
        '<option value="x">Enabled</option><option disabled value="x">Disabled duplicate</option>'
        "</select>"
    )
    snap = browser.observe()
    pick = target(snap, "Pick")
    assert [option.value for option in pick.options] == ["init", "x"]
    result = browser.execute(decide(snap, "SELECT", target=pick.id, value="x"))
    assert result.success and result.executed
    assert browser.page.locator("select").evaluate("node => node.selectedIndex") == 1


def test_browser_events_block_and_page_close_is_observed(browser, site):
    browser.open(site + "search.html")
    browser.page.set_content("<button onclick=\"window.open('about:blank')\">Popup</button>")
    snap = browser.observe()
    browser.execute(decide(snap, "CLICK", target=target(snap, "Popup").id))
    assert browser.blocked_reason
    assert browser.alive


def test_dialog_does_not_continue(browser, site):
    browser.open(site + "search.html")
    browser.page.set_content("<button onclick=\"confirm('Proceed?')\">Confirm</button>")
    snap = browser.observe()
    browser.execute(decide(snap, "CLICK", target=target(snap, "Confirm").id))
    assert browser.blocked_reason


def test_download_is_blocked(browser, site):
    browser.open(site + "search.html")
    browser.page.set_content('<a href="search.html" download="copy.html">Download</a>')
    snap = browser.observe()
    with browser.page.expect_download(timeout=3000):
        result = browser.execute(decide(snap, "CLICK", target=target(snap, "Download").id))
    assert browser.blocked_reason == "download_blocked", result


def test_upload_control_is_not_exposed(browser, site):
    browser.open(site + "search.html")
    browser.page.set_content('<input type="file" aria-label="Upload">')
    assert not browser.observe().targets


def test_open_rejects_untrusted_schemes_and_credentials(browser):
    for url in ("file:///etc/passwd", "javascript:alert(1)", "https://user:pass@example.com"):
        with pytest.raises(ValueError):
            browser.open(url)


def test_closed_page_is_not_alive(browser, site):
    browser.open(site + "search.html")
    browser.page.close()
    assert not browser.alive
    with pytest.raises(RuntimeError):
        browser.observe()


def test_snapshot_ids_are_unique_across_sessions(site):
    with BrowserSession(headless=True) as first:
        first.open(site + "search.html")
        first_id = first.observe().snapshot_id
    with BrowserSession(headless=True) as second:
        second.open(site + "search.html")
        assert first_id != second.observe().snapshot_id


def test_expired_deadline_prevents_browser_launch(site):
    browser = BrowserSession(headless=True)
    browser.deadline = time.monotonic() - 1
    with pytest.raises(TimeoutError):
        browser.open(site + "search.html")
    assert browser.page is None


def test_invalid_browser_channel_is_rejected(monkeypatch):
    monkeypatch.setenv("DOMPILOT_BROWSER_CHANNEL", "custom")
    with pytest.raises(ValueError):
        with BrowserSession(headless=True):
            pass


def test_open_lazily_starts_session(site):
    browser = BrowserSession(headless=True)
    try:
        browser.open(site + "search.html")
        assert browser.alive
        assert target(browser.observe(), "Search")
    finally:
        browser.close()
