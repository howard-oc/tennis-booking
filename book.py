"""
Tennis court booker — Hilly Fields, LTA booking system.
Books the next Sunday's court: tries 08:00-09:00 first, falls back to 07:00-08:00.
Logs "NO SLOTS" and exits cleanly if neither is available.

Run standalone:   python book.py
Run with --debug: python book.py --debug   (headed browser, pauses on error)
"""

import asyncio
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PWTimeout, async_playwright

load_dotenv(Path(__file__).parent / ".env")

# ── Config ───────────────────────────────────────────────────────────────────

LTA_EMAIL    = os.getenv("LTA_EMAIL", "")
LTA_PASSWORD = os.getenv("LTA_PASSWORD", "")

# Payment details — set as GitHub Secrets; if absent the browser stays open for manual pay
CARD_NUMBER = os.getenv("CARD_NUMBER", "")
CARD_EXPIRY = os.getenv("CARD_EXPIRY", "")   # MM/YY
CARD_CVV    = os.getenv("CARD_CVV", "")
CARD_NAME   = os.getenv("CARD_NAME", "")

VENUE_ID  = "hilly-fields_73a37dbe-18fe-4ac6-8a16-073105b5e9fe"
VENUE_LAT = "51.4723111"
VENUE_LON = "-0.0290865"

# Preferred slot; FALLBACK_TIME used if preferred is unavailable
TARGET_TIME   = "08:00"
FALLBACK_TIME = "07:00"

# LTA opens court bookings this many days before the play date
BOOKING_WINDOW_DAYS = 6

SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tennis")


# ── Helpers ───────────────────────────────────────────────────────────────────

def next_sunday() -> date:
    # Allow TARGET_DATE env var to override (set by workflow_dispatch input)
    override = os.getenv("TARGET_DATE", "").strip()
    if override:
        return date.fromisoformat(override)
    today = date.today()
    days_ahead = (6 - today.weekday()) % 7
    return today if days_ahead == 0 else today + timedelta(days=days_ahead)


def booking_url(for_date: date) -> str:
    return (
        f"https://www.lta.org.uk/book/courts/{VENUE_ID}/"
        f"?latitude={VENUE_LAT}&longitude={VENUE_LON}"
        f"&date={for_date.isoformat()}"
    )


async def screenshot(page, name: str):
    path = SCREENSHOTS_DIR / f"{name}.png"
    await page.screenshot(path=str(path), full_page=True)
    log.info("Screenshot: %s", path.name)


# ── Login ─────────────────────────────────────────────────────────────────────

async def login(page):
    log.info("Logging in as %s ...", LTA_EMAIL)
    await page.goto("https://mylta.my.site.com/s/login/", wait_until="networkidle")
    await screenshot(page, "01-login-page")

    await page.locator("input[placeholder='Username'], input[name='username']").first.fill(LTA_EMAIL)
    await page.locator("input[type='password']").first.fill(LTA_PASSWORD)
    await page.locator("button:has-text('Log in')").first.click()
    # Wait until redirected away from the login page (Salesforce never reaches networkidle)
    try:
        await page.wait_for_url(lambda url: "/s/login" not in url, timeout=20000)
    except PWTimeout:
        pass
    await page.wait_for_load_state("load")
    await page.wait_for_timeout(1000)
    await screenshot(page, "02-post-login")

    if "/s/login" in page.url:
        raise RuntimeError("Login failed — check credentials in .env")
    log.info("Logged in OK")


# ── Booking ───────────────────────────────────────────────────────────────────

async def dismiss_cookies(page):
    try:
        btn = page.locator("button:has-text('Accept all cookies'), button:has-text('Accept all')")
        await btn.first.click(timeout=5000)
        # Wait for the banner to actually disappear
        await page.locator("#onetrust-banner-sdk, .cookie-banner, [class*='cookie']").wait_for(
            state="hidden", timeout=5000
        )
        log.info("Cookie banner dismissed")
    except PWTimeout:
        # If we can't dismiss it, scroll past it
        await page.evaluate("window.scrollBy(0, 300)")
    await page.wait_for_timeout(500)


async def book_slot(page, for_date: date, debug: bool):
    url = booking_url(for_date)
    log.info("Loading booking page for %s", for_date.strftime("%A %d %b %Y"))
    await page.goto(url, wait_until="networkidle")
    await dismiss_cookies(page)
    await page.wait_for_timeout(2000)
    await screenshot(page, "03-booking-page")

    # The real booking form uses #js-venue-booking-* selects.
    # Read what start times are available, try 08:00 then 07:00.
    avail = await page.evaluate("""() => {
        const s = document.querySelector('#js-venue-booking-start-time');
        return s ? Array.from(s.options).map(o => ({text: o.text.trim(), value: o.value})) : [];
    }""")
    # Filter out the placeholder "Please Select" entry
    real_slots = [o for o in avail if o['value']]
    log.info("Available start times: %s", [o['text'] for o in real_slots])

    # Distinguish "not open yet" from "all slots taken"
    if not real_slots:
        opens_on = for_date - timedelta(days=BOOKING_WINDOW_DAYS)
        today = date.today()
        if today < opens_on:
            log.info(
                "Booking not open yet for %s — opens %s. Nothing to do.",
                for_date.strftime("%A %d %b %Y"),
                opens_on.strftime("%A %d %b"),
            )
        else:
            await screenshot(page, "04-no-slots")
            log.warning(
                "NO SLOTS AVAILABLE for %s — booking is open but all courts taken.",
                for_date.strftime("%A %d %b %Y"),
            )
        return

    booked_time = None
    booked_value = None
    for want in [TARGET_TIME, FALLBACK_TIME]:
        match = next((o for o in real_slots if o['text'].startswith(want)), None)
        if match:
            booked_time = want
            booked_value = match['value']
            break

    if booked_time is None:
        await screenshot(page, "04-no-slots")
        log.warning(
            "NO SLOTS AVAILABLE for %s — slots exist (%s) but neither %s nor %s is among them.",
            for_date.strftime("%A %d %b %Y"),
            [o['text'] for o in real_slots],
            TARGET_TIME, FALLBACK_TIME,
        )
        return

    slot_end = f"{int(booked_time[:2]) + 1:02d}:00"
    end_value = booked_value.replace(f"T{booked_time}", f"T{slot_end}")
    log.info("Selecting slot %s-%s (value=%s)", booked_time, slot_end, booked_value)

    # Fill form via JS and dispatch change events so the page reacts
    filled = await page.evaluate(f"""() => {{
        function set(id, val) {{
            const el = document.querySelector(id);
            if (!el) return 'missing:' + id;
            el.value = val;
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return 'ok';
        }}
        const r1 = set('#js-venue-booking-start-time', '{booked_value}');
        const r2 = set('#js-venue-booking-end-time', '{end_value}');
        // Pick Court 2 (has 07:00 slot); fall back to Court 1
        const courtSel = document.querySelector('#js-venue-booking-court');
        let court = '';
        if (courtSel) {{
            const c2 = Array.from(courtSel.options).find(o => o.value === 'Court 2');
            const c1 = Array.from(courtSel.options).find(o => o.value === 'Court 1');
            const chosen = c2 || c1;
            if (chosen) {{ courtSel.value = chosen.value; court = chosen.value; }}
            courtSel.dispatchEvent(new Event('change', {{bubbles: true}}));
        }}
        return {{r1, r2, court}};
    }}""")
    log.info("Form fill result: %s", filled)
    await page.wait_for_timeout(1000)
    await screenshot(page, "04-form-filled")

    # Click Book court — try by text, then by JS form submit
    clicked = await page.evaluate("""() => {
        const btn = [...document.querySelectorAll('button, input[type=submit]')]
            .find(b => (b.textContent || b.value || '').toLowerCase().includes('book court'));
        if (btn) { btn.click(); return btn.textContent || btn.value; }
        const form = document.querySelector('#js-venue-booking-start-time')?.closest('form');
        if (form) { form.submit(); return 'form.submit()'; }
        return null;
    }""")
    log.info("Book court click: %s", clicked)

    if not clicked:
        await screenshot(page, "05-no-book-button")
        if debug:
            await page.pause()
        log.warning("Could not find 'Book court' button")
        return

    await page.wait_for_load_state("load")
    await page.wait_for_timeout(2000)
    await screenshot(page, "05-checkout")
    slot_end = f"{int(booked_time[:2]) + 1:02d}:00"
    log.info("CHECKOUT — %s  %s-%s  Hilly Fields", for_date, booked_time, slot_end)

    if CARD_NUMBER:
        await _pay(page, debug)
    else:
        log.info(
            "No payment details in env — browser is open for manual payment.\n"
            "  Set CARD_NUMBER / CARD_EXPIRY / CARD_CVV / CARD_NAME secrets to automate."
        )
        input("  Press Enter once you have paid (or Ctrl+C to abandon)... ")
        await screenshot(page, "06-final-state")


async def _pay(page, debug: bool):
    """Fill card details and submit. Handles plain fields and Stripe/iframe inputs."""
    log.info("Filling payment details...")

    # Address may auto-fill from profile; click "Find address" or skip if pre-filled
    try:
        await page.locator("button:has-text('Find address')").first.click(timeout=3000)
        await page.wait_for_timeout(1000)
    except PWTimeout:
        pass

    # Card details — try plain inputs first, then look inside payment iframes
    async def fill_card_field(selectors: list, value: str, label: str):
        # Try direct page inputs
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=3000)
                await loc.fill(value)
                log.info("Filled %s", label)
                return
            except PWTimeout:
                continue
        # Try inside payment iframes (Stripe, WorldPay, etc.)
        for frame in page.frames:
            for sel in selectors:
                try:
                    loc = frame.locator(sel).first
                    await loc.wait_for(state="visible", timeout=2000)
                    await loc.fill(value)
                    log.info("Filled %s (iframe: %s)", label, frame.url[:60])
                    return
                except PWTimeout:
                    continue
        log.warning("Could not find field: %s", label)

    await fill_card_field(
        ["[name*='cardNumber'], [id*='card-number'], [placeholder*='card number' i], [autocomplete='cc-number']"],
        CARD_NUMBER, "card number"
    )
    await fill_card_field(
        ["[name*='expir'], [id*='expir'], [placeholder*='MM' i], [autocomplete='cc-exp']"],
        CARD_EXPIRY, "expiry"
    )
    await fill_card_field(
        ["[name*='cvv'], [name*='cvc'], [id*='cvv'], [id*='cvc'], [autocomplete='cc-csc']"],
        CARD_CVV, "CVV"
    )
    await fill_card_field(
        ["[name*='name'], [id*='name-on-card'], [autocomplete='cc-name']"],
        CARD_NAME, "name on card"
    )

    await screenshot(page, "06-payment-filled")

    # Submit payment
    for sel in ["button:has-text('Pay now')", "button:has-text('Pay')",
                "button:has-text('Confirm')", "input[type='submit']"]:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=4000)
            await loc.click()
            log.info("Payment submitted via: %s", sel)
            break
        except PWTimeout:
            continue

    await page.wait_for_load_state("load")
    await page.wait_for_timeout(2000)
    await screenshot(page, "07-booking-complete")
    log.info("PAID AND BOOKED.")


# ── Main ──────────────────────────────────────────────────────────────────────

async def run(debug: bool = False):
    if not LTA_EMAIL or not LTA_PASSWORD:
        raise SystemExit("Set LTA_EMAIL and LTA_PASSWORD in .env before running.")

    target = next_sunday()
    log.info("Target: %s | preferred %s, fallback %s", target, TARGET_TIME, FALLBACK_TIME)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=150 if debug else 0)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        try:
            await login(page)
            await book_slot(page, target, debug)
            log.info("Done.")
        except Exception as exc:
            log.error("Failed: %s", exc)
            await screenshot(page, "error-state")
            if debug:
                await page.pause()
            raise
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(run(debug="--debug" in sys.argv))
