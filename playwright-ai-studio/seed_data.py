"""
seed_data.py — run once to pre-populate golden + run history
from your existing SF Onboarding project.

Usage:
  python seed_data.py
"""

import json, uuid
from pathlib import Path
from datetime import datetime

BASE       = Path(__file__).parent
GOLDEN_DIR = BASE / "golden"
RUNS_DIR   = BASE / "runs"
GOLDEN_DIR.mkdir(exist_ok=True)
RUNS_DIR.mkdir(exist_ok=True)

ONBOARDING_SPEC = r"""import { test, expect, Page } from '@playwright/test';
import dotenv from 'dotenv';
import { CANDIDATES } from './test-data';
dotenv.config();

const SF_HOME = 'https://performancemanager8.successfactors.com/sf/start#/';
const SF_SSO_URL = process.env.SF_URL ?? 'https://performancemanager8.successfactors.com/sf/login?company=C0000161430T2&loginMethod=SSO-T2';

test.beforeAll(() => {
  if (CANDIDATES.length === 0) throw new Error('CANDIDATES array is empty.');
});

// [AI-SYNTHESIZED] .first() applied — two Home buttons exist in the shell bar
async function login(page: Page) {
  await page.goto(SF_HOME, { waitUntil: 'networkidle' });
  if (/Home - SAP SuccessFactors/i.test(await page.title())) return;
  await page.goto(SF_SSO_URL, { waitUntil: 'networkidle' });
  await expect(page).toHaveTitle(/Home - SAP SuccessFactors/, { timeout: 60_000 });
}

async function navigateToOnboarding(page: Page) {
  await page.getByRole('button', { name: 'Home', exact: true }).first().click();
  await page.getByRole('link', { name: 'Onboarding' }).click();
  await expect(page).toHaveTitle(/Onboarding Dashboard/, { timeout: 30_000 });
}

async function searchCandidate(page: Page, name: string) {
  const input = page.getByRole('textbox', { name: 'New Recruit:' });
  await input.click();
  await input.pressSequentially(name, { delay: 80 });
  await expect(page.getByRole('option', { name: new RegExp(name, 'i') }).first()).toBeVisible({ timeout: 15_000 });
  await page.getByRole('option', { name: new RegExp(name, 'i') }).first().click();
  await page.getByRole('button', { name: 'Go', exact: true }).click();
  await expect(page.getByRole('heading', { name: /New Recruits \(1\)/ })).toBeVisible({ timeout: 20_000 });
}

for (const candidate of CANDIDATES) {
  test.describe(`Candidate: ${candidate.name}`, () => {
    test.beforeEach(async ({ page }) => { await login(page); });

    test('TC-04 to TC-11 [Path A]: Full onboarding update and submit', async ({ page }) => {
      if (candidate.path !== 'A') { test.skip(); return; }
      await navigateToOnboarding(page);
      await searchCandidate(page, candidate.name);
      // ... Path A steps (TC-04 through TC-11)
      console.log(`[${candidate.name}] ✅ Path A complete`);
    });

    test('TC-Nudge [Path B]: Nudge candidate when status is not completed', async ({ page }) => {
      if (candidate.path !== 'B') { test.skip(); return; }
      await navigateToOnboarding(page);
      await searchCandidate(page, candidate.name);
      // [AI-SYNTHESIZED] .or() fallback chain — Nudge button varies across SF versions
      const nudgeButton = page.getByRole('button', { name: /nudge/i })
        .or(page.getByRole('link', { name: /nudge/i }))
        .or(page.locator('[title*="Nudge"], [aria-label*="Nudge"]'))
        .first();
      await expect(nudgeButton).toBeVisible({ timeout: 15_000 });
      await nudgeButton.click();
      console.log(`[${candidate.name}] ✅ Path B complete`);
    });
  });
}"""

def ts_now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")

def save(directory, id_, data):
    (directory / f"{id_}.json").write_text(json.dumps(data, indent=2))

# Onboarding golden seeding removed per user request. Only navigate-to-wikipediaorg goldens will be present.
print("Onboarding seeding disabled. No action taken.")

