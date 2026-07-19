#!/usr/bin/env python3
"""
Playwright E2E test of the scheduling assistant UI (backend on :8000).

Walks the real patient journey in a headless browser:
  1. App loads - sidebar shows office rules + campaign patients
  2. Click a patient pill -> OOP answer with real dollar amounts
  3. Ask to reschedule -> slot pills render as buttons
  4. Click a slot pill -> appointment is rescheduled, confirmation shows

Screenshots land in ./screenshots/; exits non-zero on any failed step.
"""

import os
import sys

from playwright.sync_api import expect, sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(HERE, "screenshots")
os.makedirs(SHOTS, exist_ok=True)
BASE = os.environ.get("APP_URL", "http://localhost:8000")
steps = []


def step(name, ok, detail=""):
    steps.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail if not ok else ''}")


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(BASE, timeout=30000)

    # 1. app + sidebar
    expect(page.locator(".sidebar h1")).to_contain_text("Green River Dental", timeout=15000)
    patients = page.locator(".patient")
    expect(patients.first).to_be_visible(timeout=15000)
    n = patients.count()
    page.screenshot(path=f"{SHOTS}/01_home.png", full_page=True)
    step("app loads with patient sidebar", n == 20, f"{n} patients")

    # 2. patient pill -> OOP answer
    page.locator(".patient", has_text="Zvonko Nikolich").click()
    reply = page.locator(".msg.assistant").last
    expect(reply).to_contain_text("$", timeout=120000)
    text = reply.inner_text()
    page.screenshot(path=f"{SHOTS}/02_oop_answer.png", full_page=True)
    step("patient click yields OOP quote", "437.50" in text or "437" in text, text[:120])

    # 3. reschedule request -> slot pills
    page.fill(".composer input",
              "This is Zvonko Nikolich - I need to move my crown. What slots are open Thursday or Friday?")
    page.click(".composer button")
    pills = page.locator(".slot-pill")
    expect(pills.first).to_be_visible(timeout=120000)
    n_pills = pills.count()
    labels = [pills.nth(i).inner_text() for i in range(min(n_pills, 5))]
    page.screenshot(path=f"{SHOTS}/03_slot_pills.png", full_page=True)
    step("slot pills render", n_pills > 0, f"{n_pills} pills: {labels}")

    # all offered crown slots must respect the 3pm cutoff
    import re
    def before_3pm(label):
        m = re.search(r"(\d+):(\d+) (AM|PM)", label)
        h = int(m.group(1)) % 12 + (12 if m.group(3) == "PM" else 0)
        return h < 15
    all_labels = [pills.nth(i).inner_text() for i in range(n_pills)]
    step("every offered crown slot starts before 3pm", all(before_3pm(l) for l in all_labels),
         [l for l in all_labels if not before_3pm(l)])

    # 4. click a pill -> reschedule completes
    pills.first.click()
    page.wait_for_timeout(500)
    last = page.locator(".msg.assistant").last
    expect(last).to_contain_text(re.compile(r"(resched|moved|confirm|booked)", re.I), timeout=180000)
    page.screenshot(path=f"{SHOTS}/04_rescheduled.png", full_page=True)
    step("slot pill click completes reschedule", True, last.inner_text()[:120])

    browser.close()

ok = sum(1 for _, p, _ in steps if p)
print(f"\nE2E: {ok}/{len(steps)} steps passed - screenshots in {SHOTS}/")
sys.exit(0 if ok == len(steps) else 1)
