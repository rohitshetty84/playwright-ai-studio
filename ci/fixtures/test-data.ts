/**
 * Stub CANDIDATES fixture used by AI-generated specs that do
 *   `import { CANDIDATES } from './test-data';`
 *
 * In a real environment this list comes from your test-management system
 * (or a CSV / Excel export). In CI we ship a minimal stub so the specs at
 * least resolve their imports — they may still fail at runtime if they
 * require real candidate data in your target system.
 *
 * To override per-environment, set the CANDIDATES_JSON env var to a JSON
 * array, e.g.:
 *   CANDIDATES_JSON='[{"name":"Rosa Philp","path":"A"}]' npx playwright test
 */

export interface Candidate {
  name: string;
  path: 'A' | 'B';
}

const DEFAULT_CANDIDATES: Candidate[] = [
  { name: 'Rosa Philp',      path: 'A' },
  { name: 'Jeremy Armstead', path: 'A' },
  { name: 'Test Onb123',     path: 'B' },
];

function parseFromEnv(): Candidate[] | null {
  const raw = process.env.CANDIDATES_JSON;
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed) && parsed.every(c => c && c.name && c.path)) {
      return parsed as Candidate[];
    }
    console.warn('[test-data] CANDIDATES_JSON parsed but did not match expected shape — using defaults');
  } catch (e) {
    console.warn('[test-data] CANDIDATES_JSON is not valid JSON — using defaults');
  }
  return null;
}

export const CANDIDATES: Candidate[] = parseFromEnv() ?? DEFAULT_CANDIDATES;
