#!/usr/bin/env python3
#encoding=utf-8
"""platforms/eztable.py -- EZTABLE restaurant booking (tw.eztable.com).

Flow on https://tw.eztable.com/restaurant/{id}:
    Stage 6  party size    -> <select> whose first option is "請選擇用餐人數"
    Stage 4  date          -> react-day-picker cells, td[data-day="YYYY-MM-DD"]
    Stage 5  time slot     -> buttons labelled "HH:MM"
    Stage 10 next step     -> "填寫付款資訊" button, opens /restaurant/{id}/booking/form
    Stage 11 booking form  -> login (LINE / SMS) or payment; notify only, the user
                              finishes login and payment by hand.

Settings reused from the shared schema:
    ticket_number                    party size
    date_auto_select.date_keyword    dates, e.g. "2026-11-27","11/28","週六"
    area_auto_select.area_keyword    time slots, e.g. "18:00","18","11:30-12:30"
    date_auto_fallback / area_auto_fallback
    advanced.auto_reload_page_interval (0 = never reload)
"""

import json
import re
import time

import util
from nodriver_common import (
    CONST_FROM_TOP_TO_BOTTOM,
    CONST_NATIVE_INPUT_SETTER_JS,
    play_sound_while_ordering,
    send_discord_notification,
    send_telegram_notification,
)


__all__ = [
    "nodriver_eztable_assign_ticket_number",
    "nodriver_eztable_date_auto_select",
    "nodriver_eztable_area_auto_select",
    "nodriver_eztable_confirm_order",
    "nodriver_eztable_auto_fill",
    "nodriver_eztable_booking",
    "nodriver_eztable_main",
]

CONST_EZTABLE_PLATFORM_NAME = "EZTABLE"
CONST_EZTABLE_NEXT_BUTTON_TEXT = "填寫付款資訊"
CONST_EZTABLE_PARTY_PLACEHOLDER = "用餐人數"
CONST_EZTABLE_FULL_MARKERS = ("已滿", "請洽客服")
CONST_EZTABLE_LOGIN_MARKERS = ("簡訊驗證", "LINE 快速登入")
CONST_EZTABLE_MAX_MONTH_NAV = 2
CONST_EZTABLE_MAX_NEXT_CLICKS = 3
CONST_EZTABLE_NEXT_CLICK_COOLDOWN = 3.0
# The panel re-renders for a moment after each pick; an empty read in that
# window is not "sold out", so no reload until the page has settled.
CONST_EZTABLE_SETTLE_SECONDS = 2.0
CONST_EZTABLE_MAX_FILL_TRIES = 5
# Booker fields on /booking/form. Only these are filled; the cardholder name and
# cardholder email belong to the payment section and stay with the user.
CONST_EZTABLE_FORM_FIELDS = (
    ("real_name", 'input[name="bookName"]'),
    ("phone", 'input[type="tel"][autocomplete="tel"]'),
    ("email", 'input[name="bookEmail"]'),
)

_state = {}

# Shared JS fragment: locate the visible booking panel. The page renders the
# panel several times (desktop, mobile, bottom sheet) and hides the unused
# copies, so pick the visible party-size <select> and walk up to the ancestor
# that also holds the calendar. Paste inside an IIFE.
_JS_FIND_PANEL = '''
        const isVisible = (el) => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };
        const findPanel = () => {
            const sel = [...document.querySelectorAll('select')].find((s) =>
                isVisible(s) && s.options.length > 0
                && s.options[0].text.includes(PLACEHOLDER));
            let panel = sel || null;
            while (panel && !panel.querySelector('.rdp-root')) {
                panel = panel.parentElement;
            }
            return { sel: sel || null, panel: panel };
        };
'''


def _js_with_panel(body):
    """Wrap a JS body in an IIFE that has isVisible / findPanel available."""
    placeholder = json.dumps(CONST_EZTABLE_PARTY_PLACEHOLDER)
    return ("(() => {\n        const PLACEHOLDER = " + placeholder + ";\n"
            + _JS_FIND_PANEL + body + "\n    })()")


_JS_READ_PANEL = _js_with_panel('''
        const found = findPanel();
        if (!found.panel) return JSON.stringify({ found: false });
        const panel = found.panel;
        const sel = found.sel;
        const days = [...panel.querySelectorAll('td[data-day]')]
            .filter((td) => td.dataset.disabled !== 'true')
            .map((td) => {
                const btn = td.querySelector('button');
                return { day: td.dataset.day,
                         label: btn ? (btn.getAttribute('aria-label') || '') : '' };
            });
        const selectedTd = panel.querySelector('td.rdp-selected[data-day]');
        const slots = [...panel.querySelectorAll('button')]
            .filter((b) => /^\\d{1,2}:\\d{2}$/.test(b.innerText.trim()))
            .map((b) => ({ text: b.innerText.trim(),
                           selected: !b.classList.contains('outline'),
                           disabled: b.disabled || /disable/i.test(b.className) }));
        const nextMonth = panel.querySelector('button.rdp-button_next');
        const nextStep = [...document.querySelectorAll('button')].find((b) =>
            isVisible(b) && b.innerText.trim() === NEXT_TEXT);
        return JSON.stringify({
            found: true,
            party: { value: sel.value,
                     options: [...sel.options].map((o) => ({ value: o.value,
                         text: o.text, disabled: o.disabled })) },
            days: days,
            selectedDay: selectedTd ? selectedTd.dataset.day : '',
            slots: slots,
            canNextMonth: !!nextMonth
                && nextMonth.getAttribute('aria-disabled') !== 'true',
            nextStepReady: !!nextStep && !nextStep.disabled,
        });
''').replace("NEXT_TEXT", json.dumps(CONST_EZTABLE_NEXT_BUTTON_TEXT))


async def _eztable_read_panel(tab):
    result = await tab.evaluate(_JS_READ_PANEL)
    if isinstance(result, str):
        result = json.loads(result)
    return result if isinstance(result, dict) else {"found": False}


async def _eztable_run_panel_action(tab, body):
    """Run a JS body against the visible panel; returns True on success."""
    result = await tab.evaluate(_js_with_panel('''
        const found = findPanel();
        if (!found.panel) return false;
        const panel = found.panel;
        const sel = found.sel;
''' + body))
    return result is True


def _get_keyword_items(keyword_string):
    keyword_string = (keyword_string or "").strip()
    if not keyword_string:
        return []
    items = util.parse_keyword_string_to_array(keyword_string)
    if not items:
        items = [kw.strip() for kw in keyword_string.split(',') if kw.strip()]
    return [str(item).strip() for item in items if str(item).strip()]


def _get_day_tokens(day, label):
    """All spellings a user may type for one calendar day."""
    tokens = {day}
    parts = day.split("-")
    if len(parts) == 3:
        y, m, d = parts
        mi, di = int(m), int(d)
        tokens.update({
            f"{y}/{m}/{d}", f"{m}/{d}", f"{m}-{d}", f"{mi}/{di}",
            f"{mi}-{di}", f"{mi}月{di}日", f"{y}{m}{d}",
        })
    match = re.search(r"星期([日一二三四五六])", label or "")
    if match:
        tokens.update({f"星期{match.group(1)}", f"週{match.group(1)}"})
    return tokens


def _to_minutes(hhmm):
    try:
        hour, minute = hhmm.strip().split(":")
        return int(hour) * 60 + int(minute)
    except (ValueError, AttributeError):
        return None


def _is_slot_match(token, slot_text):
    """Match one keyword token against a slot such as "18:00".

    "18:00" exact, "18" any slot in that hour, "11:30-12:30" inclusive range.
    """
    token = token.strip()
    slot_minutes = _to_minutes(slot_text)
    if slot_minutes is None or not token:
        return False
    if "-" in token or "~" in token:
        start, _, end = token.replace("~", "-").partition("-")
        low, high = _to_minutes(start), _to_minutes(end)
        return low is not None and high is not None and low <= slot_minutes <= high
    if ":" in token:
        return _to_minutes(token) == slot_minutes
    return token.isdigit() and int(token) == slot_minutes // 60


def _match_items(keyword_items, candidates, is_token_match):
    """OR across keyword items, AND across space-separated tokens in one item.

    The first keyword item with any match wins, as on the other platforms.
    """
    for item in keyword_items:
        tokens = [t for t in item.split(" ") if t]
        matched = [c for c in candidates
                   if tokens and all(is_token_match(t, c) for t in tokens)]
        if matched:
            return matched
    return []


def _log_on_change(debug, key, message):
    """The main loop runs every ~50 ms; log a status line only when it changes."""
    last_logs = _state.setdefault("last_logs", {})
    if last_logs.get(key) != message:
        last_logs[key] = message
        debug.log(message)


def _reset_page_state():
    _state["month_nav"] = 0


async def _eztable_reload_if_due(tab, config_dict, reason):
    """Reload the restaurant page at most once per reload interval."""
    debug = util.create_debug_logger(config_dict)
    interval = util.get_reload_interval(config_dict)
    if not interval or interval <= 0:
        return False
    now = time.monotonic()
    if now - _state.get("last_action", 0) < CONST_EZTABLE_SETTLE_SECONDS:
        return False
    if now - _state.get("last_reload", 0) < interval:
        return False
    debug.log(f"[EZTABLE RELOAD] {reason}, reloading page")
    _state["last_reload"] = now
    _reset_page_state()
    await tab.reload()
    return True


# Stage 6: party size
async def nodriver_eztable_assign_ticket_number(tab, config_dict, panel_state):
    """Return "done", "changed" or "full"."""
    debug = util.create_debug_logger(config_dict)
    party_size = config_dict.get("ticket_number", 2)
    pattern = re.compile(rf"^\s*{int(party_size)}\s*人")
    option = next((o for o in panel_state["party"]["options"]
                   if pattern.match(o.get("text", ""))), None)
    if option is None:
        _log_on_change(debug, "party", f"[EZTABLE PARTY] no option for {party_size} people")
        return "full"
    if option.get("disabled") or any(m in option["text"] for m in CONST_EZTABLE_FULL_MARKERS):
        _log_on_change(debug, "party", f"[EZTABLE PARTY] not available: {option['text']}")
        return "full"
    _log_on_change(debug, "party", f"[EZTABLE PARTY] available: {option['text']}")
    if panel_state["party"]["value"] == option["value"]:
        return "done"
    ok = await _eztable_run_panel_action(tab, f'''
        const setter = Object.getOwnPropertyDescriptor(
            window.HTMLSelectElement.prototype, 'value').set;
        setter.call(sel, {json.dumps(option["value"])});
        sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
        return sel.value === {json.dumps(option["value"])};
    ''')
    debug.log(f"[EZTABLE PARTY] set {option['text']}: {ok}")
    return "changed" if ok else "full"


# Stage 4: date selection
async def nodriver_eztable_date_auto_select(tab, config_dict, panel_state):
    """Return "done", "changed", or "none" (no usable date on this page)."""
    debug = util.create_debug_logger(config_dict)
    days = panel_state.get("days", [])
    keyword_items = _get_keyword_items(
        config_dict.get("date_auto_select", {}).get("date_keyword", ""))

    if keyword_items:
        matched = _match_items(
            keyword_items, days,
            lambda token, d: token in _get_day_tokens(d["day"], d["label"]))
    else:
        matched = list(days)

    if not matched:
        if panel_state.get("canNextMonth") and _state.get("month_nav", 0) < CONST_EZTABLE_MAX_MONTH_NAV:
            _state["month_nav"] = _state.get("month_nav", 0) + 1
            debug.log("[EZTABLE DATE] no match in this month, trying next month")
            await _eztable_run_panel_action(tab, '''
        const btn = panel.querySelector('button.rdp-button_next');
        if (!btn) return false;
        btn.click();
        return true;
            ''')
            return "changed"
        if days and config_dict.get("date_auto_fallback", False):
            debug.log("[EZTABLE DATE] date_auto_fallback on, using any open date")
            matched = list(days)
        else:
            return "none"

    if panel_state.get("selectedDay") in [d["day"] for d in matched]:
        return "done"

    mode = config_dict.get("date_auto_select", {}).get("mode", CONST_FROM_TOP_TO_BOTTOM)
    target = util.get_target_item_from_matched_list(matched, mode)
    if not target:
        return "none"
    ok = await _eztable_run_panel_action(tab, f'''
        const btn = panel.querySelector('td[data-day=' + JSON.stringify({json.dumps(target["day"])}) + '] button');
        if (!btn || btn.disabled) return false;
        btn.click();
        return true;
    ''')
    debug.log(f"[EZTABLE DATE] select {target['day']}: {ok}")
    return "changed" if ok else "none"


# Stage 5: time slot selection
async def nodriver_eztable_area_auto_select(tab, config_dict, panel_state):
    """Return "done", "changed", or "none" (no usable time slot)."""
    debug = util.create_debug_logger(config_dict)
    slots = [s for s in panel_state.get("slots", []) if not s.get("disabled")]
    if not slots:
        return "none"
    keyword_items = _get_keyword_items(
        config_dict.get("area_auto_select", {}).get("area_keyword", ""))

    if keyword_items:
        matched = _match_items(keyword_items, slots,
                               lambda token, s: _is_slot_match(token, s["text"]))
        if not matched and config_dict.get("area_auto_fallback", False):
            debug.log("[EZTABLE SLOT] area_auto_fallback on, using any open slot")
            matched = slots
    else:
        matched = slots
    if not matched:
        return "none"

    if any(s.get("selected") for s in matched):
        return "done"

    mode = config_dict.get("area_auto_select", {}).get("mode", CONST_FROM_TOP_TO_BOTTOM)
    target = util.get_target_item_from_matched_list(matched, mode)
    if not target:
        return "none"
    ok = await _eztable_run_panel_action(tab, f'''
        const want = {json.dumps(target["text"])};
        const btn = [...panel.querySelectorAll('button')].find((b) =>
            b.innerText.trim() === want);
        if (!btn || btn.disabled) return false;
        btn.click();
        return true;
    ''')
    debug.log(f"[EZTABLE SLOT] select {target['text']}: {ok}")
    return "changed" if ok else "none"


# Stage 10: open the booking form. This only moves to the next page; the order
# itself is created after the user logs in and pays by hand.
async def nodriver_eztable_confirm_order(tab, config_dict):
    debug = util.create_debug_logger(config_dict)
    now = time.monotonic()
    if _state.get("next_clicks", 0) >= CONST_EZTABLE_MAX_NEXT_CLICKS:
        return False
    if now - _state.get("last_next_click", 0) < CONST_EZTABLE_NEXT_CLICK_COOLDOWN:
        return False
    _state["next_clicks"] = _state.get("next_clicks", 0) + 1
    _state["last_next_click"] = now
    result = await tab.evaluate('''
        (() => {
            const want = ''' + json.dumps(CONST_EZTABLE_NEXT_BUTTON_TEXT) + ''';
            const btn = [...document.querySelectorAll('button')].find((b) => {
                const r = b.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && b.innerText.trim() === want;
            });
            if (!btn || btn.disabled) return false;
            btn.click();
            return true;
        })()
    ''')
    debug.log(f"[EZTABLE NEXT] click next step ({_state['next_clicks']}): {result is True}")
    return result is True


async def _eztable_restaurant_page(tab, url, config_dict):
    debug = util.create_debug_logger(config_dict)
    if _state.get("last_url") != url:
        _state["last_url"] = url
        _state["next_clicks"] = 0
        _reset_page_state()

    panel_state = await _eztable_read_panel(tab)
    if not panel_state.get("found"):
        return False

    stages = (
        (nodriver_eztable_assign_ticket_number, "party size not available"),
        (nodriver_eztable_date_auto_select, "no matching date"),
        (nodriver_eztable_area_auto_select, "no matching time slot"),
    )
    for stage_func, reload_reason in stages:
        result = await stage_func(tab, config_dict, panel_state)
        if result == "changed":
            _state["last_action"] = time.monotonic()
            return False
        if result != "done":
            await _eztable_reload_if_due(tab, config_dict, reload_reason)
            return False

    # Re-read: the next-step button appears only after a slot is chosen.
    panel_state = await _eztable_read_panel(tab)
    if panel_state.get("nextStepReady"):
        return await nodriver_eztable_confirm_order(tab, config_dict)
    _log_on_change(debug, "next", "[EZTABLE] waiting for next-step button")
    return False


# Stage 8: fill booker name / phone / email from the shared contact settings.
# Fields that already hold a value (EZTABLE pre-fills the login phone) are left
# untouched, and nothing in the payment section is filled.
async def nodriver_eztable_auto_fill(tab, config_dict):
    debug = util.create_debug_logger(config_dict)
    if _state.get("form_filled") or _state.get("fill_tries", 0) >= CONST_EZTABLE_MAX_FILL_TRIES:
        return
    contact = config_dict.get("contact", {})
    wanted = [(key, selector, str(contact.get(key, "")).strip())
              for key, selector in CONST_EZTABLE_FORM_FIELDS]
    wanted = [(key, selector, value) for key, selector, value in wanted if value]
    if not wanted:
        _state["form_filled"] = True
        return
    # The form renders in steps; space the tries out so they span ~5 seconds.
    now = time.monotonic()
    if now - _state.get("last_fill_try", 0) < 1.0:
        return
    _state["last_fill_try"] = now
    _state["fill_tries"] = _state.get("fill_tries", 0) + 1

    result = await tab.evaluate('''
        (() => {
''' + CONST_NATIVE_INPUT_SETTER_JS + '''
            const wanted = ''' + json.dumps([[k, s, v] for k, s, v in wanted]) + ''';
            const out = {};
            for (const [key, selector, value] of wanted) {
                const matches = [...document.querySelectorAll(selector)];
                if (matches.length !== 1) { out[key] = 'not_found'; continue; }
                const input = matches[0];
                if (input.value && input.value.trim()) { out[key] = 'kept'; continue; }
                out[key] = setNativeInputValue(input, value) ? 'filled' : 'failed';
            }
            return JSON.stringify(out);
        })()
    ''')
    status = json.loads(result) if isinstance(result, str) else {}
    # Log field names and outcome only, never the personal values themselves.
    debug.log(f"[EZTABLE FILL] try {_state['fill_tries']}: {status}")
    if status and all(v in ("filled", "kept") for v in status.values()):
        _state["form_filled"] = True


# Stage 11: booking form -- login or payment, both left to the user.
async def nodriver_eztable_booking(tab, config_dict):
    debug = util.create_debug_logger(config_dict)
    body_text = await tab.evaluate("(() => document.body ? document.body.innerText : '')()")
    body_text = body_text if isinstance(body_text, str) else ""
    if not body_text.strip():
        return False

    is_login = any(m in body_text for m in CONST_EZTABLE_LOGIN_MARKERS)
    play_sound = config_dict.get("advanced", {}).get("play_sound", {})
    if is_login:
        if not _state.get("login_notified"):
            _state["login_notified"] = True
            print("[EZTABLE] 請在瀏覽器中以 LINE 或簡訊驗證登入，登入後會繼續。")
            if play_sound.get("ticket", True):
                play_sound_while_ordering(config_dict)
        return False

    await nodriver_eztable_auto_fill(tab, config_dict)

    if not _state.get("order_notified"):
        _state["order_notified"] = True
        print("[EZTABLE] 已進入訂位表單，請確認資料並手動完成付款。")
        debug.log("[EZTABLE] booking form reached, waiting for user")
        if play_sound.get("order", True):
            play_sound_while_ordering(config_dict)
        send_discord_notification(config_dict, "order", CONST_EZTABLE_PLATFORM_NAME)
        send_telegram_notification(config_dict, "order", CONST_EZTABLE_PLATFORM_NAME)
    return True


# Stage 3: page monitor / URL router
async def nodriver_eztable_main(tab, url, config_dict):
    debug = util.create_debug_logger(config_dict)
    if "month_nav" not in _state:
        _state.update({
            "last_url": "",
            "month_nav": 0,
            "last_reload": 0,
            "last_action": 0,
            "next_clicks": 0,
            "last_next_click": 0,
            "login_notified": False,
            "order_notified": False,
        })

    path = url.split("?", 1)[0].split("#", 1)[0]
    try:
        if "/booking/success" in path:
            if not _state.get("success_logged"):
                _state["success_logged"] = True
                print("[EZTABLE] 訂位完成。")
            return True
        if "/booking/form" in path:
            return await nodriver_eztable_booking(tab, config_dict)
        if re.search(r"/restaurant/\d+/?$", path):
            _state["login_notified"] = False
            _state["order_notified"] = False
            _state["form_filled"] = False
            _state["fill_tries"] = 0
            _state["last_fill_try"] = 0
            return await _eztable_restaurant_page(tab, url, config_dict)
    except Exception as exc:
        debug.log(f"[EZTABLE MAIN] Error: {exc}")
    return False
