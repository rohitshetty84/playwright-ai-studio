"""
Playwright AI Studio — Python/FastAPI backend
Azure OpenAI powered test synthesis & auto-healing
"""

import os, json, uuid, re, subprocess, tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional
import requests

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import AzureOpenAI
from dotenv import load_dotenv

# ── Import improved healing engine ─────────────────────────────────────────────
from healing_engine import ErrorSignature, generate_targeted_healing_prompt, analyze_healing_history

BASE = Path(__file__).parent
ROOT_ENV = BASE.parent / ".env"
load_dotenv()
if ROOT_ENV.exists():
    load_dotenv(ROOT_ENV, override=False)

# ── Azure OpenAI client ───────────────────────────────────────────────────────
client = AzureOpenAI(
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01"),
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
)
DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

# ── Storage paths ─────────────────────────────────────────────────────────────
BASE           = Path(__file__).parent
GOLDEN_DIR     = BASE / "golden"
RUNS_DIR       = BASE / "runs"
HEALING_DIR    = BASE / "healing_history"  # Track all healing attempts
GOLDEN_DIR.mkdir(exist_ok=True)
RUNS_DIR.mkdir(exist_ok=True)
HEALING_DIR.mkdir(exist_ok=True)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Playwright AI Studio", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

# ── Pydantic models ───────────────────────────────────────────────────────────
class SynthesizeRequest(BaseModel):
    test_case: str
    script_fragment: Optional[str] = ""

class SaveGoldenRequest(BaseModel):
    name: str
    description: str
    code: str
    browsers: list[str] = ["msedge"]
    analysis: Optional[dict] = {}

class RunRequest(BaseModel):
    golden_id: str
    browser: str = "msedge"
    candidates: list[dict]   # [{name, path, status, duration, error?}]

class HealRequest(BaseModel):
    golden_id: str

class PromoteGoldenRequest(BaseModel):
    code: str

class TriggerCIRequest(BaseModel):
    golden_ids: str  # comma-separated list of golden IDs

class ValidateHealRequest(BaseModel):
    golden_id: str
# ── Azure OpenAI helper ───────────────────────────────────────────────────────
def ask_llm(system: str, user: str, max_tokens: int = 1500) -> str:
    try:
        resp = client.chat.completions.create(
            model=DEPLOYMENT,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            max_tokens=max_tokens,
            temperature=0.2,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Azure OpenAI error: {e}")

# ── File helpers ──────────────────────────────────────────────────────────────
def load_goldens() -> list[dict]:
    out = []
    for f in sorted(GOLDEN_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            pass
    return out

def load_runs() -> list[dict]:
    out = []
    for f in sorted(RUNS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            pass
    return out

def save_json(directory: Path, id: str, data: dict):
    (directory / f"{id}.json").write_text(json.dumps(data, indent=2))

def ts_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")

# ── Healing History Helper Functions ──────────────────────────────────────────
def load_healing_history(golden_id: str) -> list:
    """Load all healing attempts for a golden"""
    path = HEALING_DIR / f"{golden_id}_history.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []

def save_healing_attempt(golden_id: str, attempt: dict):
    """Save a healing attempt with metadata"""
    history = load_healing_history(golden_id)
    attempt["attemptNumber"] = len(history) + 1
    attempt["timestamp"] = ts_now()
    history.append(attempt)
    save_json(HEALING_DIR, f"{golden_id}_history", history)
    print(f"[healing] Recorded attempt #{attempt['attemptNumber']} for golden {golden_id}")

def get_healing_failures_for_error(golden_id: str, error_msg: str) -> list:
    """Get all failed healing attempts for a specific error"""
    history = load_healing_history(golden_id)
    return [h for h in history if h.get("error") == error_msg and not h.get("succeeded", True)]

def is_healing_stuck(golden_id: str) -> dict:
    """Check if healing has failed multiple times for same error"""
    history = load_healing_history(golden_id)
    if len(history) < 3:
        return {"stuck": False}

    # Group by error type
    errors = {}
    for h in history:
        error = h.get("error", "unknown")
        if error not in errors:
            errors[error] = []
        if not h.get("succeeded", True):
            errors[error].append(h)

    # Check if any error has 3+ failed attempts
    for error, attempts in errors.items():
        if len(attempts) >= 3:
            return {
                "stuck": True,
                "error": error,
                "failedAttempts": len(attempts),
                "recommendation": "MANUAL_FIX_NEEDED",
                "history": attempts
            }

    return {"stuck": False}

# ── LOCAL VALIDATION: Run tests locally for instant feedback ──────────────────
async def validate_test_locally(test_code: str, golden_id: str) -> dict:
    """
    Run a Playwright test locally and return results immediately.

    This allows auto-heal to test fixes in seconds instead of waiting for GitHub Actions.

    Args:
        test_code: The TypeScript test code to validate
        golden_id: The golden ID (for temp file naming)

    Returns:
        {
            "status": "PASS" | "FAIL" | "ERROR",
            "duration": 2.1,
            "error": null or error message,
            "passed": true | false
        }
    """
    try:
        # Create temp test file with TypeScript extension
        # Use system temp directory (automatically created by OS)
        with tempfile.NamedTemporaryFile(
            mode='w',
            suffix=f'_{golden_id}_validate.spec.ts',
            delete=False
        ) as f:
            f.write(test_code)
            temp_file = f.name

        try:
            # Call the Node.js validation script
            root_project = BASE.parent  # Go up from playwright-ai-studio
            validate_script = root_project / "validate-test.js"

            if not validate_script.exists():
                return {
                    "status": "ERROR",
                    "error": f"validate-test.js not found at {validate_script}",
                    "duration": 0,
                    "passed": False
                }

            # Verify temp file exists and is readable
            temp_file_path = Path(temp_file)
            if not temp_file_path.exists():
                return {
                    "status": "ERROR",
                    "error": f"Temp file not found: {temp_file}",
                    "duration": 0,
                    "passed": False
                }

            print(f"[validation] Temp file created: {temp_file}")
            print(f"[validation] File size: {temp_file_path.stat().st_size} bytes")
            print(f"[validation] Running: node {validate_script} {temp_file}")

            result = subprocess.run(
                ['node', str(validate_script), temp_file],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(root_project)
            )

            print(f"[validation] Exit code: {result.returncode}")
            print(f"[validation] Stdout: {result.stdout[:500]}")
            if result.stderr:
                print(f"[validation] Stderr: {result.stderr[:500]}")

            # Parse JSON result
            try:
                output = json.loads(result.stdout)
                return output
            except json.JSONDecodeError as e:
                # If JSON parsing fails, return the raw output as error
                return {
                    "status": "ERROR",
                    "error": f"Failed to parse validation result: {result.stdout[:500]}",
                    "duration": 0,
                    "passed": False
                }

        finally:
            # Clean up temp file
            try:
                Path(temp_file).unlink()
            except Exception as cleanup_err:
                print(f"[validation] Warning: Could not delete temp file {temp_file}: {cleanup_err}")

    except subprocess.TimeoutExpired:
        return {
            "status": "TIMEOUT",
            "error": "Test validation timed out (>60 seconds)",
            "duration": 60,
            "passed": False
        }
    except Exception as e:
        return {
            "status": "ERROR",
            "error": f"Validation error: {str(e)}",
            "duration": 0,
            "passed": False
        }

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse(BASE / "static" / "index.html")

@app.get("/api/goldens")
async def get_goldens():
    return load_goldens()

@app.get("/api/runs")
async def get_runs():
    return load_runs()

# ─ Step 1: Analyse test case ──────────────────────────────────────────────────
@app.post("/api/synthesize/analyse")
async def analyse(req: SynthesizeRequest):
    raw = ask_llm(
        system="""You are a Playwright test architect specialising in SAP SuccessFactors automation.
Analyse the test case description and any script fragment provided.
Return ONLY a JSON object (no markdown) with these keys:
  "steps"             – array of step name strings
  "selectors"         – array of key selector descriptions
  "risks"             – array of flakiness risk strings
  "healingStrategies" – array of recommended healing patterns""",
        user=f"Test case: {req.test_case}\n\nExisting script:\n{req.script_fragment or '(none)'}",
        max_tokens=600,
    )
    # Strip markdown fences if model adds them
    clean = re.sub(r"```(?:json)?|```", "", raw).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        return {
            "steps": ["Login", "Navigate to Onboarding", "Search candidate", "Update fields", "Submit"],
            "selectors": ["role-based locators", "getByRole", "getByText"],
            "risks": ["dynamic content loading", "timing", "multiple matching elements"],
            "healingStrategies": [".first() scoping", ".or() fallback chains", "waitForLoadState"],
        }

# ─ Step 2: Synthesize full script ─────────────────────────────────────────────
@app.post("/api/synthesize/generate")
async def generate(req: SynthesizeRequest):
    analysis_raw = ask_llm(
        system="Return ONLY JSON. No markdown.",
        user=f"Analyse: {req.test_case}",
        max_tokens=400,
    )

    code = ask_llm(
        system="""You are a senior Playwright TypeScript engineer for SAP SuccessFactors automation.
Generate a complete, production-ready Playwright test file.

Rules:
- Use TypeScript with proper imports from '@playwright/test'
- Include a login() helper using storageState from user.json
- Use getByRole() selectors; apply .first() when multiple matches are possible
- Apply .or() fallback chains for dynamic elements (e.g. Nudge button)
- Add waitForLoadState('networkidle') after navigation
- Log each TC step with console.log('[step] ✅ description')
- Use a for..of loop over CANDIDATES from './test-data'
- Mark path='A' vs path='B' branching clearly
- Add [AI-SYNTHESIZED] inline comments explaining selector choices
Output ONLY the TypeScript code. No markdown fences.""",
        user=f"Test case: {req.test_case}\n\nScript hints:\n{req.script_fragment or '(none)'}",
        max_tokens=1500,
    )
    return {"code": code}

# ─ Save as Golden ──────────────────────────────────────────────────────────────
@app.post("/api/goldens")
async def create_golden(req: SaveGoldenRequest):
    gid = str(uuid.uuid4())[:8]
    golden = {
        "id": gid,
        "name": req.name,
        "description": req.description,
        "code": req.code,
        "browsers": req.browsers,
        "analysis": req.analysis,
        "createdAt": ts_now(),
        "healCount": 0,
        "lastHealed": None,
        "status": "active",
        "steps": len(req.analysis.get("steps", [])) or 5,
    }
    save_json(GOLDEN_DIR, gid, golden)
    return golden

# ─ Record a test run ──────────────────────────────────────────────────────────
@app.post("/api/runs")
async def record_run(req: RunRequest):
    # Look up golden by ID if it exists (optional for CI/CD robustness)
    golden = next((g for g in load_goldens() if g["id"] == req.golden_id), None)

    rid = str(uuid.uuid4())[:8]
    run = {
        "id": rid,
        "goldenId": req.golden_id,
        "goldenName": golden["name"] if golden else req.golden_id,  # Use ID as name if golden not found
        "browser": req.browser,
        "runAt": ts_now(),
        "candidates": req.candidates,
    }
    save_json(RUNS_DIR, rid, run)

    # ── NEW: Detect if this is a post-healing run and check if healing succeeded ──
    if golden and golden.get("healCount", 0) > 0:
        # This golden has been healed before
        has_failures = any(c.get("status") == "fail" for c in req.candidates)
        error_msg = None

        if has_failures:
            # Find the first error
            for c in req.candidates:
                if c.get("status") == "fail" and c.get("error"):
                    error_msg = c.get("error")
                    break

            # Check if this is the same error as before
            history = load_healing_history(req.golden_id)
            if history:
                last_attempt = history[-1]
                if last_attempt.get("error") == error_msg:
                    # HEALING FAILED - same error persists!
                    save_healing_attempt(req.golden_id, {
                        "fix": "Previous healing attempt",
                        "error": error_msg,
                        "succeeded": False,
                        "result": "Same error persists after healing",
                        "testResult": "FAIL"
                    })
                    print(f"[healing] ❌ HEALING FAILED for {req.golden_id}: Same error persists")
                else:
                    # Different error - healing helped with previous issue
                    save_healing_attempt(req.golden_id, {
                        "fix": "Previous healing attempt",
                        "error": error_msg,
                        "succeeded": False,
                        "result": f"New error appeared: {error_msg}",
                        "testResult": "FAIL"
                    })
                    print(f"[healing] ⚠️  New error for {req.golden_id}: {error_msg}")
        else:
            # All tests passed! Healing succeeded!
            if history:
                save_healing_attempt(req.golden_id, {
                    "fix": "Previous healing attempt",
                    "error": "NONE",
                    "succeeded": True,
                    "result": "All tests passed!",
                    "testResult": "PASS"
                })
                print(f"[healing] ✅ HEALING SUCCEEDED for {req.golden_id}!")

    # Log for debugging
    status = "✓ recorded" if golden else "⚠ recorded (golden not found, using ID as name)"
    print(f"[api/runs] {status} — ID={rid}, golden={req.golden_id}, candidates={len(req.candidates)}")

    return run

# ─ Auto-Heal ──────────────────────────────────────────────────────────────────
@app.post("/api/heal/{golden_id}")
async def heal(golden_id: str):
    golden = next((g for g in load_goldens() if g["id"] == golden_id), None)
    if not golden:
        raise HTTPException(status_code=404, detail="Golden not found")

    # Collect errors from all runs for this golden
    errors = []
    latest_error = None
    for run in load_runs():
        if run.get("goldenId") == golden_id:
            for c in run.get("candidates", []):
                if c.get("status") == "fail" and c.get("error"):
                    errors.append(f"[{c['name']} Path {c['path']}] {c['error']}")
                    latest_error = c.get("error")

    error_summary = "\n".join(errors) if errors else "Selector timeout on dynamic elements."

    # ── NEW: Check healing history and learn from failures ──────────────────
    healing_history = load_healing_history(golden_id)
    learning_context = ""

    if latest_error and len(healing_history) > 0:
        # Get previous failed attempts for this same error
        failed_attempts = get_healing_failures_for_error(golden_id, latest_error)
        if len(failed_attempts) > 0:
            learning_context = f"""
⚠️  LEARNING FROM PAST FAILURES:
This error has been seen {len(failed_attempts)} time(s) before.

Previous failed fixes:
"""
            for attempt in failed_attempts[-2:]:  # Show last 2 failures
                learning_context += f"  - Attempt #{attempt['attemptNumber']}: {attempt.get('fix', 'Unknown fix')}\n"

            learning_context += f"""
DO NOT repeat these approaches. Instead, try a fundamentally different strategy.

For "Locators must belong to the same frame" error:
  ❌ DO NOT: Mix getByRole() with locator() in .or() chains
  ❌ DO NOT: Chain selectors that operate in different frame contexts
  ✅ DO: Use only page.locator() chains consistently
  ✅ DO: Keep all locators within the same frame context
  ✅ DO: Use .first() to disambiguate, not .or() for different selector types
"""

    system_prompt = """You are a Playwright auto-healing expert.
Given failure error messages and the original golden TypeScript script, produce an improved script.
For every fix, add an inline comment starting exactly with [AI-HEAL] explaining what changed and why.
Key healing patterns:
  - Use only page.locator() chains to stay in same frame
  - .first() for ambiguous multi-match locators
  - waitForLoadState for timing gaps
  - try/catch with fallback click strategies

CRITICAL: Avoid mixing different selector types (getByRole + locator) in same chain.
All selectors in a chain must operate in the same frame context.
Output ONLY the TypeScript code. No markdown."""

    healed_code = ask_llm(
        system=system_prompt,
        user=f"""Errors:\n{error_summary}\n\nOriginal golden script:\n{golden['code']}{learning_context}""",
        max_tokens=1500,
    )

    # Strip markdown fences if LLM wrapped code in them
    healed_code = re.sub(r"```(?:typescript|ts|js)?[\n]?", "", healed_code).strip()

    # CRITICAL FIX: Remove trailing [AI-HEAL] comments and junk after closing braces
    # Azure OpenAI sometimes appends comments/summaries after code ends, breaking syntax
    # Strategy: Remove everything after the last complete test block

    # First, find and keep only up to the last closing });
    lines = healed_code.split('\n')
    kept_lines = []
    found_closing = False

    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        # Look for the closing pattern of a test describe block: });
        if not found_closing and '});' in line:
            # Keep this line and everything before it
            kept_lines = lines[:i+1]
            found_closing = True
            break

    # If we found a proper closing, use it; otherwise use original
    if found_closing:
        healed_code = '\n'.join(kept_lines).rstrip()

    # Additional safeguard: Remove any trailing lines that are [AI-HEAL] comments
    while True:
        lines = healed_code.split('\n')
        last_line = lines[-1] if lines else ''
        if re.match(r'^\s*(\*+\s*)?\[AI-HEAL\]', last_line):
            lines.pop()
            healed_code = '\n'.join(lines).rstrip()
        else:
            break

    print(f"[heal] Generated fix for golden {golden_id} (attempt #{len(healing_history) + 1})")

    # Generate a plain-English diff summary
    diff_summary = ask_llm(
        system="Return ONLY a JSON array of strings. Each string = one change made. Max 6 items. No markdown.",
        user=f"Summarise the healing changes made:\nErrors: {error_summary}\nHealed code excerpt: {healed_code[:800]}",
        max_tokens=300,
    )
    try:
        changes = json.loads(re.sub(r"```(?:json)?|```", "", diff_summary).strip())
    except Exception:
        changes = ["Applied .first() to ambiguous role selectors", "Added .or() fallback for Nudge button", "Added waitForLoadState after navigation"]

    return {"healedCode": healed_code, "changes": changes}

# ─ NEW: Heal + Validate Locally (Instant Feedback) ───────────────────────────
@app.post("/api/heal-and-validate/{golden_id}")
async def heal_and_validate(golden_id: str):
    """
    NEW WORKFLOW: Generate healed code AND run it locally for instant feedback.

    Instead of: Heal → Promote → Wait 5+ min for GitHub
    Now: Heal → Validate locally (2-5 sec) → See pass/fail → Decide to promote

    Returns:
        {
            "goldenId": "...",
            "healedCode": "...",
            "testResult": "PASS" | "FAIL" | "ERROR",
            "duration": 2.1,
            "error": null or error message,
            "passed": true | false,
            "readyToPromote": true | false,
            "message": "✅ Test passed! Ready to promote." or error
        }
    """
    golden = next((g for g in load_goldens() if g["id"] == golden_id), None)
    if not golden:
        raise HTTPException(status_code=404, detail="Golden not found")

    try:
        # Step 1: Collect errors and diagnose root cause
        errors = []
        latest_error = None
        for run in load_runs():
            if run.get("goldenId") == golden_id:
                for c in run.get("candidates", []):
                    if c.get("status") == "fail" and c.get("error"):
                        errors.append(f"[{c['name']} Path {c['path']}] {c['error']}")
                        latest_error = c.get("error")

        error_summary = "\n".join(errors) if errors else "Selector timeout on dynamic elements."

        healing_history = load_healing_history(golden_id)
        learning_context = ""

        # ── NEW: Diagnose root cause using ErrorSignature ──────────────────────
        diagnosis = ErrorSignature.diagnose(latest_error or error_summary, golden["code"])
        root_cause = diagnosis.get("root_cause", "UNKNOWN")
        confidence = diagnosis.get("confidence", 0.0)

        print(f"[heal-validate] Root cause diagnosis: {root_cause} (confidence: {confidence:.0%})")
        print(f"[heal-validate] Evidence: {diagnosis.get('evidence', 'No evidence')}")

        # ── Analyze healing history for patterns ──────────────────────────────
        history_analysis = analyze_healing_history(healing_history)
        if history_analysis.get("needs_manual_review"):
            print(f"[heal-validate] ⚠️ Healing stuck - manual review recommended")

        # Build learning context from history
        if len(healing_history) > 0:
            learning_context = f"""
⚠️  LEARNING FROM PAST FAILURES (Attempt #{len(healing_history) + 1}):
Previous attempts: {len(healing_history)}
Root cause identified: {root_cause} (confidence: {confidence:.0%})

Recent attempts:
"""
            for attempt in healing_history[-3:]:
                learning_context += f"  - Attempt #{attempt.get('attemptNumber', '?')}: {attempt.get('rootCause', 'Unknown')}\n"

            learning_context += f"""
Strategy: Apply targeted fix for '{root_cause}' instead of generic selector fixes.
"""

        # ── Generate targeted healing prompt based on diagnosis ────────────────
        system_prompt, user_prompt = generate_targeted_healing_prompt(
            latest_error or error_summary,
            golden["code"],
            diagnosis,
            learning_context
        )

        healed_code = ask_llm(
            system=system_prompt,
            user=user_prompt,
            max_tokens=1500,
        )

        # Strip markdown fences if LLM wrapped code in them
        healed_code = re.sub(r"```(?:typescript|ts|js)?[\n]?", "", healed_code).strip()

        # CRITICAL FIX: Remove trailing [AI-HEAL] comments and junk after closing braces
        # Azure OpenAI sometimes appends comments/summaries after code ends, breaking syntax
        # Strategy: Remove everything after the last complete test block

        # First, find and keep only up to the last closing });
        lines = healed_code.split('\n')
        kept_lines = []
        found_closing = False

        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            # Look for the closing pattern of a test describe block: });
            if not found_closing and '});' in line:
                # Keep this line and everything before it
                kept_lines = lines[:i+1]
                found_closing = True
                break

        # If we found a proper closing, use it; otherwise use original
        if found_closing:
            healed_code = '\n'.join(kept_lines).rstrip()

        # Additional safeguard: Remove any trailing lines that are [AI-HEAL] comments
        while True:
            lines = healed_code.split('\n')
            last_line = lines[-1] if lines else ''
            if re.match(r'^\s*(\*+\s*)?\[AI-HEAL\]', last_line):
                lines.pop()
                healed_code = '\n'.join(lines).rstrip()
            else:
                break

        print(f"[heal-validate] Generated targeted fix for '{root_cause}' (attempt #{len(healing_history) + 1})")

        # Step 2: Run test locally
        print(f"[heal-validate] Running test locally for golden {golden_id}...")
        validation_result = await validate_test_locally(healed_code, golden_id)

        # Step 3: Return result with code + pass/fail
        response = {
            "goldenId": golden_id,
            "healedCode": healed_code,
            "testResult": validation_result["status"],
            "duration": validation_result.get("duration", 0),
            "error": validation_result.get("error"),
            "passed": validation_result.get("passed", False),
            "readyToPromote": validation_result["status"] == "PASS",
        }

        # Step 4: Record healing attempt with diagnosis
        save_healing_attempt(golden_id, {
            "fix": f"Applied targeted fix for root cause: {root_cause}",
            "rootCause": root_cause,
            "confidence": confidence,
            "error": latest_error or error_summary,
            "succeeded": validation_result["status"] == "PASS",
            "testResult": validation_result["status"],
            "duration": validation_result.get("duration", 0),
            "newError": validation_result.get("error") if validation_result["status"] != "PASS" else None
        })

        if validation_result["status"] == "PASS":
            response["message"] = f"✅ Test PASSED in {validation_result.get('duration', 0):.1f}s! Ready to promote."
            print(f"[heal-validate] ✅ LOCAL TEST PASSED for {golden_id}!")
        else:
            error_msg = validation_result.get("error", "Unknown error")
            response["message"] = f"❌ Test FAILED: {error_msg}"
            print(f"[heal-validate] ❌ LOCAL TEST FAILED for {golden_id}: {error_msg}")
            response["diagnosis"] = {
                "rootCause": root_cause,
                "confidence": confidence,
                "evidence": diagnosis.get("evidence")
            }

        return response

    except Exception as e:
        print(f"[heal-validate] Error: {str(e)}")
        return {
            "error": str(e),
            "goldenId": golden_id,
            "testResult": "ERROR",
            "message": f"Validation error: {str(e)}"
        }, 500

# ─ Promote healed code as new Golden ─────────────────────────────────────────
@app.patch("/api/goldens/{golden_id}/promote")
async def promote_healed(golden_id: str, body: PromoteGoldenRequest):
    goldens = load_goldens()
    golden = next((g for g in goldens if g["id"] == golden_id), None)
    if not golden:
        raise HTTPException(status_code=404, detail="Golden not found")

    if not body.code.strip():
        raise HTTPException(status_code=400, detail="Promoted code cannot be empty")

    golden["code"] = body.code
    golden["healCount"] = golden.get("healCount", 0) + 1
    golden["lastHealed"] = ts_now()
    save_json(GOLDEN_DIR, golden_id, golden)

    # ── NEW: Save this healing attempt to history ──────────────────────────
    # Find what the fix was by comparing code or using a marker
    save_healing_attempt(golden_id, {
        "fix": "Generated fix from Azure OpenAI",
        "error": "TBD - will be confirmed when tests run",
        "succeeded": None,  # Pending - will be updated when test runs
        "result": "Promoted and awaiting test results",
        "testResult": "PENDING"
    })
    print(f"[promote] Saved healing attempt #{golden.get('healCount')} for {golden_id}")

    # ── Auto-trigger GitHub Actions to test the healed golden ──────────────────
    # This ensures the healed code is tested with the updated golden file
    workflow_result = {"status": "skipped", "message": "GitHub workflow not configured"}
    try:
        print(f"[promote] Auto-triggering workflow for healed golden: {golden_id}")
        workflow_result = dispatch_github_workflow({"golden_ids": golden_id})
        print(f"[promote] Workflow triggered successfully: {workflow_result.get('message')}")
    except HTTPException as e:
        # Workflow dispatch failed but golden was saved successfully
        print(f"[promote] Warning: Could not trigger workflow: {e.detail}")
        workflow_result = {"status": "failed", "message": str(e.detail)}
    except Exception as e:
        print(f"[promote] Unexpected error triggering workflow: {e}")
        workflow_result = {"status": "failed", "message": str(e)}

    return {
        "golden": golden,
        "workflowTriggered": workflow_result.get("status") == "success",
        "workflowMessage": workflow_result.get("message", "Unknown"),
    }

# ─ Check Healing Status & Escalation ────────────────────────────────────────
@app.get("/api/goldens/{golden_id}/healing-status")
async def get_healing_status(golden_id: str):
    """Get healing history and check if escalation is needed"""
    golden = next((g for g in load_goldens() if g["id"] == golden_id), None)
    if not golden:
        raise HTTPException(status_code=404, detail="Golden not found")

    history = load_healing_history(golden_id)
    stuck = is_healing_stuck(golden_id)

    return {
        "goldenId": golden_id,
        "goldenName": golden.get("name"),
        "healAttempts": len(history),
        "healCount": golden.get("healCount", 0),
        "lastHealed": golden.get("lastHealed"),
        "isEscalated": stuck.get("stuck", False),
        "escalationReason": stuck.get("error") if stuck.get("stuck") else None,
        "failedAttempts": stuck.get("failedAttempts", 0),
        "recommendation": stuck.get("recommendation", "CONTINUE_AUTO_HEALING"),
        "recentHistory": history[-5:] if history else [],  # Last 5 attempts
    }

# ─ GitHub workflow dispatch helpers ─────────────────────────────────────────
def parse_github_remote(url: str):
    if not url:
        return None
    if url.startswith("git@github.com:"):
        path = url.split(":", 1)[1]
    elif url.startswith("https://github.com/"):
        path = url[len("https://github.com/"):]
    elif url.startswith("ssh://git@github.com/"):
        path = url.split("github.com/", 1)[1]
    else:
        return None

    if path.endswith(".git"):
        path = path[:-4]
    parts = path.strip("/").split("/")
    return tuple(parts) if len(parts) == 2 else None


def get_github_config():
    # Reload env each time so updates to .env are picked up while the server runs.
    if ROOT_ENV.exists():
        load_dotenv(ROOT_ENV, override=True)

    gh_token = os.getenv("GITHUB_TOKEN")
    gh_owner = os.getenv("GITHUB_OWNER")
    gh_repo = os.getenv("GITHUB_REPO")
    gh_workflow = os.getenv("GITHUB_WORKFLOW", "playwright-test.yml")
    gh_branch = os.getenv("GITHUB_BRANCH", "main")

    if not gh_owner or not gh_repo:
        try:
            remote_url = subprocess.check_output(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=BASE.parent,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            parsed = parse_github_remote(remote_url)
            if parsed:
                gh_owner, gh_repo = parsed
        except Exception:
            pass

    if not all([gh_token, gh_owner, gh_repo]):
        raise HTTPException(
            status_code=500,
            detail="GitHub credentials not configured in .env (GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO)"
        )

    return gh_token, gh_owner, gh_repo, gh_workflow, gh_branch


def dispatch_github_workflow(inputs: dict):
    gh_token, gh_owner, gh_repo, gh_workflow, gh_branch = get_github_config()
    url = f"https://api.github.com/repos/{gh_owner}/{gh_repo}/actions/workflows/{gh_workflow}/dispatches"
    headers = {
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    payload = {
        "ref": gh_branch,
        "inputs": inputs,
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)

        if response.status_code == 204:
            return {
                "status": "success",
                "message": "GitHub workflow dispatched",
                "inputs": inputs,
            }

        # Handle error responses with better detail
        try:
            error_detail = response.json().get("message", response.text)
        except Exception:
            error_detail = response.text

        if response.status_code == 401:
            raise HTTPException(status_code=401, detail="Invalid GitHub token")
        elif response.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Workflow not found: {gh_workflow}")
        else:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"GitHub API error: {error_detail}"
            )

    except requests.Timeout:
        raise HTTPException(
            status_code=504,
            detail="GitHub API timeout (>15s) - workflow may still be triggered"
        )
    except requests.ConnectionError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot reach GitHub API - check internet connection: {str(e)}"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected error triggering workflow: {str(e)}"
        )


# ─ Trigger CI run by golden ID ───────────────────────────────────────────────
@app.post("/api/trigger-ci/{golden_id}")
async def trigger_ci(golden_id: str):
    """Trigger a GitHub Actions workflow for a specific golden test"""
    golden = next((g for g in load_goldens() if g["id"] == golden_id), None)
    if not golden:
        raise HTTPException(status_code=404, detail="Golden not found")

    # Use consistent input format for all workflows
    inputs = {"golden_name": golden["name"], "golden_id": golden_id}
    result = dispatch_github_workflow(inputs)
    return {**result, "golden_id": golden_id, "golden_name": golden["name"]}


# ─ Trigger CI run by golden_ids string ─────────────────────────────────────────
@app.post("/api/trigger-ci")
async def trigger_ci_ids(body: TriggerCIRequest):
    """Trigger workflow for multiple golden tests (comma-separated IDs)"""
    golden_ids = [gid.strip() for gid in body.golden_ids.split(",") if gid.strip()]
    if not golden_ids:
        raise HTTPException(status_code=400, detail="Please provide one or more golden_ids")

    known_ids = {g["id"] for g in load_goldens()}
    invalid = [gid for gid in golden_ids if gid not in known_ids]
    if invalid:
        raise HTTPException(status_code=404, detail=f"Unknown golden IDs: {', '.join(invalid)}")

    # For multi-trigger, use first golden's name as reference
    golden_names = [g["name"] for g in load_goldens() if g["id"] in golden_ids]
    inputs = {"golden_ids": ",".join(golden_ids)}
    result = dispatch_github_workflow(inputs)
    return {**result, "golden_ids": golden_ids, "golden_count": len(golden_ids)}


# ─ Get workflow run status ────────────────────────────────────────────────────
@app.get("/api/workflow-status/{golden_id}")
async def get_workflow_status(golden_id: str):
    """Get status of the most recent workflow run for a golden"""
    try:
        gh_token, gh_owner, gh_repo, gh_workflow, gh_branch = get_github_config()

        # Get recent workflow runs
        url = f"https://api.github.com/repos/{gh_owner}/{gh_repo}/actions/workflows/{gh_workflow}/runs"
        headers = {"Authorization": f"token {gh_token}", "Accept": "application/vnd.github.v3+json"}

        response = requests.get(url, headers=headers, timeout=10, params={"per_page": 10})

        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Failed to fetch workflow runs: {response.text}"
            )

        runs = response.json().get("workflow_runs", [])

        # Find run for this golden_id by checking workflow inputs
        matching_run = None
        for run in runs:
            inputs = run.get("inputs", {})
            if inputs.get("golden_id") == golden_id:
                matching_run = run
                break

        if not matching_run:
            return {
                "status": "not_found",
                "message": "No workflow run found for this golden",
                "golden_id": golden_id,
            }

        return {
            "status": "found",
            "golden_id": golden_id,
            "run_id": matching_run["id"],
            "run_number": matching_run["run_number"],
            "name": matching_run["name"],
            "conclusion": matching_run.get("conclusion"),  # null=running, success/failure
            "status": matching_run["status"],  # queued, in_progress, completed
            "created_at": matching_run["created_at"],
            "updated_at": matching_run["updated_at"],
            "html_url": matching_run["html_url"],
            "github_link": matching_run["html_url"],
            "display_title": matching_run.get("display_title", matching_run["name"]),
        }
    except requests.Timeout:
        raise HTTPException(status_code=504, detail="GitHub API timeout")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error fetching workflow status: {str(e)}")


# ─ Health check endpoints ──────────────────────────────────────────────────────
@app.get("/api/health")
async def health_check():
    """Basic health check"""
    return {"status": "healthy", "service": "Playwright AI Studio"}


@app.get("/api/health/github")
async def check_github_health():
    """Verify GitHub credentials and API access"""
    try:
        gh_token, gh_owner, gh_repo, gh_workflow, gh_branch = get_github_config()

        # Check if repo is accessible
        url = f"https://api.github.com/repos/{gh_owner}/{gh_repo}"
        headers = {"Authorization": f"token {gh_token}"}
        response = requests.get(url, headers=headers, timeout=5)

        if response.status_code == 200:
            repo_data = response.json()
            return {
                "status": "healthy",
                "owner": gh_owner,
                "repo": gh_repo,
                "workflow": gh_workflow,
                "branch": gh_branch,
                "repo_url": repo_data.get("html_url"),
            }
        elif response.status_code == 401:
            raise HTTPException(status_code=401, detail="Invalid GitHub token - check GITHUB_TOKEN in .env")
        elif response.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Repository not found: {gh_owner}/{gh_repo}")
        else:
            raise HTTPException(status_code=response.status_code, detail=f"GitHub API returned {response.status_code}")

    except requests.Timeout:
        raise HTTPException(status_code=504, detail="GitHub API timeout")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GitHub check failed: {str(e)}")


# ── Dev entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
