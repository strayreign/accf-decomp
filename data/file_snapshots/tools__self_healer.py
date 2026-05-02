#!/usr/bin/env python3
"""
self_healer.py  —  Autonomous self-healing & improvement engine for the ACCF
decompilation autopilot.

Architecture
────────────
  Incident  → KnowledgeBase lookup  → (matched?) apply fix
                                    → (no match) WebSearcher + LLMAdvisor
                                              → PatchApplicator
  Every outcome (success / fail) is stored in KnowledgeBase for future runs.

Public API (import anywhere):
    from tools.self_healer import get_engine, report_incident, record_match

    report_incident("ida_crash",  {"rc": 3221225477, "file": "autopilot.py"})
    report_incident("import_err", {"module": "httpx"})
    record_match("fn_80451234", 87.3)

The engine runs its analysis in a background thread — callers are never
blocked.  The engine self-improves by patching source files in the project
when its LLM advisor generates a valid, AST-clean patch.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# ── project root ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR     = PROJECT_ROOT / "data"
LOGS_DIR     = PROJECT_ROOT / "logs"
KB_FILE      = DATA_DIR / "healer_kb.json"
PATCH_BACKUP = DATA_DIR / "patch_backups"
LOG_FILE     = LOGS_DIR / "self_healer.log"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
PATCH_BACKUP.mkdir(parents=True, exist_ok=True)

OLLAMA_14B = "http://localhost:11434"
OLLAMA_7B  = "http://localhost:11435"

# ── logging ───────────────────────────────────────────────────────────────────
_log_lock = threading.Lock()

def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════════════════════════

class IncidentType(str, Enum):
    IDA_CRASH       = "ida_crash"
    IDA_TIMEOUT     = "ida_timeout"
    IDA_PORT_STUCK  = "ida_port_stuck"
    GHIDRA_DOWN     = "ghidra_down"
    OLLAMA_TIMEOUT  = "ollama_timeout"
    OLLAMA_DOWN     = "ollama_down"
    IMPORT_ERROR    = "import_error"
    COMPILE_ERROR   = "compile_error"
    SYNTAX_ERROR    = "syntax_error"
    LOW_MATCH_RATE  = "low_match_rate"
    WATCHDOG_STUCK  = "watchdog_stuck"
    GENERIC         = "generic"


@dataclass
class Incident:
    kind:    IncidentType
    details: dict[str, Any] = field(default_factory=dict)
    ts:      float           = field(default_factory=time.time)


@dataclass
class Action:
    kind:   str             # write_recovery | restart_service | pip_install |
                            # kill_port | wait | write_config | llm_patch |
                            # run_command
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class KBEntry:
    incident_kind: str
    signature:     dict[str, Any]     # subset of details used for matching
    actions:       list[dict]         # serialised Action list
    successes:     int = 0
    failures:      int = 0

    @property
    def score(self) -> float:
        total = self.successes + self.failures
        return self.successes / total if total else 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# Knowledge base  (persisted to data/healer_kb.json)
# ═══════════════════════════════════════════════════════════════════════════════

class KnowledgeBase:
    """Thread-safe on-disk knowledge base."""

    # Built-in rules loaded once at startup
    _BUILTINS: list[dict] = [
        {
            "incident_kind": IncidentType.IDA_CRASH,
            "signature":     {"rc_hex": "c0000005"},
            "actions": [
                {"kind": "write_recovery", "params": {"action": "disable_auto_analysis"}},
                {"kind": "restart_service", "params": {"service": "ida"}},
            ],
        },
        {
            "incident_kind": IncidentType.IDA_CRASH,
            "signature":     {"rc_hex": "c00000fd"},
            "actions": [
                {"kind": "write_recovery", "params": {"action": "disable_auto_analysis"}},
                {"kind": "restart_service", "params": {"service": "ida"}},
            ],
        },
        {
            "incident_kind": IncidentType.IDA_CRASH,
            "signature":     {"rc_hex": "c0000409"},
            "actions": [
                {"kind": "write_recovery", "params": {"action": "disable_auto_analysis"}},
                {"kind": "restart_service", "params": {"service": "ida"}},
            ],
        },
        {
            "incident_kind": IncidentType.IDA_PORT_STUCK,
            "signature":     {},
            "actions": [
                {"kind": "kill_port",  "params": {"port": 8081}},
                {"kind": "wait",       "params": {"seconds": 3}},
                {"kind": "restart_service", "params": {"service": "ida"}},
            ],
        },
        {
            "incident_kind": IncidentType.IMPORT_ERROR,
            "signature":     {"module": "httpx"},
            "actions": [
                {"kind": "pip_install", "params": {"package": "httpx"}},
            ],
        },
        {
            "incident_kind": IncidentType.IMPORT_ERROR,
            "signature":     {"module": "requests"},
            "actions": [
                {"kind": "pip_install", "params": {"package": "requests"}},
            ],
        },
        {
            "incident_kind": IncidentType.OLLAMA_DOWN,
            "signature":     {},
            "actions": [
                {"kind": "run_command", "params": {"cmd": "ollama serve", "background": True}},
                {"kind": "wait",        "params": {"seconds": 5}},
            ],
        },
        {
            "incident_kind": IncidentType.OLLAMA_TIMEOUT,
            "signature":     {},
            "actions": [
                {"kind": "write_config", "params": {
                    "key": "ollama_timeout", "value": 120,
                    "file": str(DATA_DIR / "healer_config.json"),
                }},
            ],
        },
        {
            "incident_kind": IncidentType.GHIDRA_DOWN,
            "signature":     {},
            "actions": [
                {"kind": "restart_service", "params": {"service": "ghidra"}},
            ],
        },
        {
            "incident_kind": IncidentType.WATCHDOG_STUCK,
            "signature":     {},
            "actions": [
                {"kind": "run_command", "params": {
                    "cmd": "taskkill /F /IM python.exe /FI \"WINDOWTITLE eq decomp*\"",
                    "background": False,
                }},
                {"kind": "wait", "params": {"seconds": 2}},
            ],
        },
        {
            "incident_kind": IncidentType.GENERIC,
            "signature":     {"error_type": "credit_balance"},
            "actions": [
                {"kind": "write_config", "params": {
                    "key": "api_credits_exhausted", "value": True,
                    "file": str(DATA_DIR / "healer_config.json"),
                }},
            ],
        },
    ]

    def __init__(self) -> None:
        self._lock    = threading.Lock()
        self._entries: list[KBEntry] = []
        self._load()

    def _load(self) -> None:
        # Built-ins first
        for raw in self._BUILTINS:
            self._entries.append(KBEntry(
                incident_kind=raw["incident_kind"],
                signature=raw["signature"],
                actions=raw["actions"],
            ))
        # Learned entries from disk
        if KB_FILE.exists():
            try:
                data = json.loads(KB_FILE.read_text(encoding="utf-8"))
                for e in data.get("learned", []):
                    self._entries.append(KBEntry(
                        incident_kind=e["incident_kind"],
                        signature=e["signature"],
                        actions=e["actions"],
                        successes=e.get("successes", 0),
                        failures=e.get("failures", 0),
                    ))
                _log(f"[KB] loaded {len(data.get('learned', []))} learned entries")
            except Exception as exc:
                _log(f"[KB] failed to load {KB_FILE}: {exc}")

    def _save(self) -> None:
        learned = []
        for e in self._entries:
            # Only save entries that have been used (have scores)
            if e.successes + e.failures > 0:
                learned.append({
                    "incident_kind": e.incident_kind,
                    "signature":     e.signature,
                    "actions":       e.actions,
                    "successes":     e.successes,
                    "failures":      e.failures,
                })
        try:
            tmp = KB_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"learned": learned}, indent=2), encoding="utf-8")
            shutil.copy(str(tmp), str(KB_FILE))
            tmp.unlink(missing_ok=True)
        except Exception as exc:
            _log(f"[KB] save failed: {exc}")

    def _sig_match(self, entry: KBEntry, details: dict) -> bool:
        """True if every key in entry.signature matches the incident details."""
        for k, v in entry.signature.items():
            dv = details.get(k)
            # normalise rc codes: compare as lowercase hex strings
            if k in ("rc_hex",) and dv is not None:
                dv_s = format(int(dv) & 0xFFFFFFFF, "x") if isinstance(dv, int) else str(dv).lower().lstrip("0x")
                if dv_s != str(v).lower().lstrip("0x"):
                    return False
            elif str(dv).lower() != str(v).lower():
                return False
        return True

    def lookup(self, incident: Incident) -> Optional[KBEntry]:
        with self._lock:
            candidates = [
                e for e in self._entries
                if e.incident_kind == incident.kind
                and self._sig_match(e, incident.details)
            ]
            if not candidates:
                return None
            # Pick highest-scoring entry
            return max(candidates, key=lambda e: e.score)

    def add_learned(self, incident: Incident, actions: list[Action]) -> KBEntry:
        with self._lock:
            entry = KBEntry(
                incident_kind=incident.kind,
                signature=incident.details.copy(),
                actions=[{"kind": a.kind, "params": a.params} for a in actions],
            )
            self._entries.append(entry)
            self._save()
            return entry

    def record_outcome(self, entry: KBEntry, success: bool) -> None:
        with self._lock:
            if success:
                entry.successes += 1
            else:
                entry.failures += 1
            self._save()


# ═══════════════════════════════════════════════════════════════════════════════
# Web search  (DuckDuckGo HTML, no API key required)
# ═══════════════════════════════════════════════════════════════════════════════

class WebSearcher:
    _DDG_URL = "https://html.duckduckgo.com/html/"
    _TIMEOUT = 12

    def search(self, query: str, max_results: int = 5) -> list[str]:
        """Return up to max_results snippet strings."""
        try:
            import urllib.request, urllib.parse
            params = urllib.parse.urlencode({"q": query, "b": ""})
            req = urllib.request.Request(
                self._DDG_URL,
                data=params.encode(),
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; self_healer/1.0)",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._TIMEOUT) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            # extract result snippets
            snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)
            clean = []
            for s in snippets[:max_results]:
                s = re.sub(r"<[^>]+>", "", s).strip()
                if s:
                    clean.append(s)
            return clean
        except Exception as exc:
            _log(f"[WebSearcher] error: {exc}")
            return []

    def search_for_fix(self, error_summary: str) -> str:
        """Consolidated web context string for an error."""
        q = f"Python fix: {error_summary} site:stackoverflow.com OR site:github.com"
        snippets = self.search(q)
        if not snippets:
            return ""
        return "Web search results:\n" + "\n".join(f"- {s}" for s in snippets)


# ═══════════════════════════════════════════════════════════════════════════════
# LLM advisor  (Ollama, local)
# ═══════════════════════════════════════════════════════════════════════════════

class LLMAdvisor:
    _TIMEOUT = 90

    def _call(self, base_url: str, model: str, prompt: str) -> str:
        try:
            import urllib.request
            payload = json.dumps({
                "model":  model,
                "prompt": prompt,
                "stream": False,
            }).encode()
            req = urllib.request.Request(
                f"{base_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._TIMEOUT) as resp:
                data = json.loads(resp.read())
                return data.get("response", "").strip()
        except Exception as exc:
            _log(f"[LLMAdvisor] call failed ({base_url}): {exc}")
            return ""

    def _available_models(self) -> list[tuple[str, str]]:
        """Returns list of (base_url, model_name) for available Ollama instances."""
        candidates = []
        for url in (OLLAMA_14B, OLLAMA_7B):
            try:
                import urllib.request
                with urllib.request.urlopen(f"{url}/api/tags", timeout=4) as r:
                    data = json.loads(r.read())
                    models = [m["name"] for m in data.get("models", [])]
                    if models:
                        candidates.append((url, models[0]))
            except Exception:
                pass
        return candidates

    def suggest_actions(self, incident: Incident, web_ctx: str) -> list[Action]:
        """Ask LLM to suggest a list of actions to fix this incident."""
        models = self._available_models()
        if not models:
            _log("[LLMAdvisor] no Ollama models available — skipping LLM advice")
            return []

        base_url, model = models[0]
        prompt = textwrap.dedent(f"""
            You are an expert Python/reverse-engineering assistant helping fix
            an automated ACCF (Animal Crossing: City Folk) decompilation script.

            Incident type : {incident.kind}
            Incident data : {json.dumps(incident.details, indent=2)}

            {web_ctx}

            Suggest a concise ordered list of actions to fix this incident.
            Each action must be one of:
              write_recovery   params: action (string)
              restart_service  params: service ("ida"|"ghidra"|"ollama")
              pip_install      params: package (string)
              kill_port        params: port (int)
              wait             params: seconds (int)
              write_config     params: key, value, file (strings)
              run_command      params: cmd (string), background (bool)

            Respond with ONLY a JSON array of action objects.
            Example: [{{"kind":"pip_install","params":{{"package":"httpx"}}}}]
        """).strip()

        raw = self._call(base_url, model, prompt)
        # Extract JSON array from response
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if not m:
            _log(f"[LLMAdvisor] no JSON array in response: {raw[:200]}")
            return []
        try:
            items = json.loads(m.group())
            actions = []
            valid_kinds = {
                "write_recovery", "restart_service", "pip_install",
                "kill_port", "wait", "write_config", "run_command", "llm_patch",
            }
            for item in items:
                k = item.get("kind", "")
                if k in valid_kinds:
                    actions.append(Action(kind=k, params=item.get("params", {})))
            return actions
        except Exception as exc:
            _log(f"[LLMAdvisor] JSON parse error: {exc}")
            return []

    def generate_patch(self, file_path: Path, error_desc: str, web_ctx: str) -> Optional[str]:
        """
        Ask LLM to generate a unified diff patch for a Python source file.
        Returns the raw diff string, or None.
        """
        models = self._available_models()
        if not models:
            return None

        base_url, model = models[0]
        try:
            src = file_path.read_text(encoding="utf-8")
        except Exception:
            return None

        # Keep prompt compact — only send relevant lines
        lines = src.splitlines()
        total = len(lines)
        start = max(0, total - 60)
        end   = total
        snippet_lines = [f"{i+start+1}: {l}" for i, l in enumerate(lines[start:end])]
        snippet = "\n".join(snippet_lines)

        prompt = textwrap.dedent(f"""
            File: {file_path.name}  (lines {start+1}-{end} of {total})
            Error: {error_desc}

            {web_ctx}

            Relevant source lines:
            {snippet}

            Produce a minimal unified diff (--- a/  +++ b/ format) that fixes
            only the error described. Do NOT add new features. Output ONLY the
            diff block, nothing else.
        """).strip()

        raw = self._call(base_url, model, prompt)
        if "---" in raw and "+++" in raw:
            return raw
        _log(f"[LLMAdvisor] generate_patch: no diff in response")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Patch applicator  (AST-validates before writing)
# ═══════════════════════════════════════════════════════════════════════════════

class PatchApplicator:

    def _backup(self, file_path: Path) -> Path:
        ts = time.strftime("%Y%m%d_%H%M%S")
        dest = PATCH_BACKUP / f"{file_path.name}.{ts}.bak"
        shutil.copy2(str(file_path), str(dest))
        return dest

    def _ast_valid(self, source: str) -> bool:
        try:
            ast.parse(source)
            return True
        except SyntaxError:
            return False

    def _compile_check(self, source: str) -> bool:
        with tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", encoding="utf-8", delete=False
        ) as f:
            f.write(source)
            tmp = f.name
        try:
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", tmp],
                capture_output=True, timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def apply_diff(self, file_path: Path, diff_text: str) -> bool:
        """
        Apply a unified diff to file_path.
        Returns True on success.
        """
        if not file_path.exists():
            _log(f"[PatchApplicator] file not found: {file_path}")
            return False
        backup = self._backup(file_path)
        _log(f"[PatchApplicator] backed up {file_path.name} → {backup.name}")

        with tempfile.NamedTemporaryFile(
            suffix=".patch", mode="w", encoding="utf-8", delete=False
        ) as f:
            f.write(diff_text)
            patch_file = f.name

        try:
            result = subprocess.run(
                ["patch", "--forward", "-i", patch_file, str(file_path)],
                capture_output=True, timeout=15,
            )
            if result.returncode != 0:
                _log(f"[PatchApplicator] patch failed: {result.stderr.decode()[:200]}")
                shutil.copy2(str(backup), str(file_path))
                return False
        except FileNotFoundError:
            # `patch` not installed — use Python difflib apply
            _log("[PatchApplicator] `patch` binary not found; skipping diff apply")
            shutil.copy2(str(backup), str(file_path))
            return False
        except Exception as exc:
            _log(f"[PatchApplicator] exception: {exc}")
            shutil.copy2(str(backup), str(file_path))
            return False
        finally:
            try:
                os.unlink(patch_file)
            except Exception:
                pass

        # Validate the result
        try:
            new_src = file_path.read_text(encoding="utf-8")
        except Exception:
            shutil.copy2(str(backup), str(file_path))
            return False

        if not self._ast_valid(new_src) or not self._compile_check(new_src):
            _log("[PatchApplicator] AST/compile validation failed — reverting")
            shutil.copy2(str(backup), str(file_path))
            return False

        _log(f"[PatchApplicator] patch applied and validated: {file_path.name}")
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# Action executor
# ═══════════════════════════════════════════════════════════════════════════════

IDA_RECOVERY = PROJECT_ROOT / "data" / "ida_recovery.json"
GHIDRA_PORT  = 8080
IDA_PORT     = 8081


class ActionExecutor:

    def __init__(self, patcher: PatchApplicator) -> None:
        self._patcher = patcher

    def execute(self, action: Action, incident: Incident) -> bool:
        k = action.kind
        p = action.params
        try:
            if k == "write_recovery":
                return self._write_recovery(p.get("action", "disable_auto_analysis"))
            elif k == "restart_service":
                return self._restart_service(p.get("service", ""))
            elif k == "pip_install":
                return self._pip_install(p.get("package", ""))
            elif k == "kill_port":
                return self._kill_port(int(p.get("port", 0)))
            elif k == "wait":
                time.sleep(float(p.get("seconds", 2)))
                return True
            elif k == "write_config":
                return self._write_config(p)
            elif k == "run_command":
                return self._run_command(p.get("cmd", ""), bool(p.get("background", False)))
            elif k == "llm_patch":
                # Generated dynamically by LLMAdvisor — handled externally
                return False
            else:
                _log(f"[ActionExecutor] unknown action kind: {k}")
                return False
        except Exception as exc:
            _log(f"[ActionExecutor] exception in {k}: {exc}")
            return False

    def _write_recovery(self, action: str) -> bool:
        try:
            IDA_RECOVERY.parent.mkdir(parents=True, exist_ok=True)
            existing: dict = {}
            if IDA_RECOVERY.exists():
                try:
                    existing = json.loads(IDA_RECOVERY.read_text(encoding="utf-8"))
                except Exception:
                    pass
            existing[action] = True
            existing["ts"] = time.time()
            tmp = IDA_RECOVERY.with_suffix(".tmp")
            tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            shutil.copy(str(tmp), str(IDA_RECOVERY))
            tmp.unlink(missing_ok=True)
            _log(f"[ActionExecutor] wrote IDA recovery flag: {action}")
            return True
        except Exception as exc:
            _log(f"[ActionExecutor] write_recovery error: {exc}")
            return False

    def _restart_service(self, service: str) -> bool:
        _log(f"[ActionExecutor] restart_service({service}) — informational only; "
             "autopilot manages service restarts")
        return True  # autopilot handles actual restarts

    def _pip_install(self, package: str) -> bool:
        if not package:
            return False
        _log(f"[ActionExecutor] pip install {package}")
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet",
             "--break-system-packages", package],
            capture_output=True, timeout=60,
        )
        if r.returncode == 0:
            _log(f"[ActionExecutor] installed {package}")
            return True
        _log(f"[ActionExecutor] pip failed: {r.stderr.decode()[:200]}")
        return False

    def _kill_port(self, port: int) -> bool:
        if not port:
            return False
        _log(f"[ActionExecutor] killing processes on port {port}")
        # Try lsof (Linux/macOS) first, then netstat (Windows via PowerShell)
        try:
            r = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, timeout=5,
            )
            pids = r.stdout.decode().split()
            for pid in pids:
                try:
                    subprocess.run(["kill", "-9", pid.strip()], timeout=5)
                except Exception:
                    pass
            if pids:
                _log(f"[ActionExecutor] killed PIDs {pids} on port {port}")
            return True
        except FileNotFoundError:
            pass
        # Windows fallback
        try:
            ps = (
                f"$p=Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue;"
                f"if($p){{Stop-Process -Id $p.OwningProcess -Force}}"
            )
            subprocess.run(["powershell", "-Command", ps], timeout=8, capture_output=True)
            return True
        except Exception as exc:
            _log(f"[ActionExecutor] kill_port fallback error: {exc}")
            return False

    def _write_config(self, params: dict) -> bool:
        cfg_file = Path(params.get("file", str(DATA_DIR / "healer_config.json")))
        cfg: dict = {}
        if cfg_file.exists():
            try:
                cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        cfg[params["key"]] = params["value"]
        try:
            tmp = cfg_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            shutil.copy(str(tmp), str(cfg_file))
            tmp.unlink(missing_ok=True)
            _log(f"[ActionExecutor] wrote config {params['key']}={params['value']}")
            return True
        except Exception as exc:
            _log(f"[ActionExecutor] write_config error: {exc}")
            return False

    def _run_command(self, cmd: str, background: bool) -> bool:
        if not cmd:
            return False
        _log(f"[ActionExecutor] run_command: {cmd}")
        try:
            if background:
                subprocess.Popen(cmd, shell=True)
                return True
            r = subprocess.run(cmd, shell=True, timeout=30, capture_output=True)
            return r.returncode == 0
        except Exception as exc:
            _log(f"[ActionExecutor] run_command error: {exc}")
            return False


# ═══════════════════════════════════════════════════════════════════════════════
# Metrics tracker  (rolling match-rate history, improvement detection)
# ═══════════════════════════════════════════════════════════════════════════════

METRICS_FILE = DATA_DIR / "healer_metrics.json"

class MetricsTracker:

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict = self._load()

    def _load(self) -> dict:
        if METRICS_FILE.exists():
            try:
                return json.loads(METRICS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"match_rates": [], "incidents": []}

    def _save(self) -> None:
        try:
            tmp = METRICS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            shutil.copy(str(tmp), str(METRICS_FILE))
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

    def record_match(self, unit: str, pct: float) -> None:
        with self._lock:
            self._data["match_rates"].append({
                "unit": unit, "pct": pct, "ts": time.time()
            })
            # Keep last 500 only
            if len(self._data["match_rates"]) > 500:
                self._data["match_rates"] = self._data["match_rates"][-500:]
            self._save()

    def record_incident(self, incident: Incident, resolved: bool) -> None:
        with self._lock:
            self._data["incidents"].append({
                "kind":     incident.kind,
                "details":  incident.details,
                "ts":       incident.ts,
                "resolved": resolved,
            })
            if len(self._data["incidents"]) > 1000:
                self._data["incidents"] = self._data["incidents"][-1000:]
            self._save()

    def recent_avg_match(self, n: int = 20) -> float:
        with self._lock:
            rates = self._data["match_rates"][-n:]
            if not rates:
                return 0.0
            return sum(r["pct"] for r in rates) / len(rates)

    def stagnant(self, window: int = 50, threshold: float = 2.0) -> bool:
        """True if match rate hasn't improved by threshold% over the last window records."""
        with self._lock:
            rates = [r["pct"] for r in self._data["match_rates"][-window:]]
            if len(rates) < window:
                return False
            first_half  = sum(rates[:window//2]) / (window//2)
            second_half = sum(rates[window//2:]) / (window//2)
            return (second_half - first_half) < threshold


# ═══════════════════════════════════════════════════════════════════════════════
# Self-healing engine  (main coordinator)
# ═══════════════════════════════════════════════════════════════════════════════



#!/usr/bin/env python3
"""
Additions to self_healer.py:
  - ResourceHunter  : proactively searches the web for any data that could help
  - ProactiveAnalyzer: periodic analysis loop; prints visible status; triggers hunts
"""

RESOURCES_DIR = DATA_DIR / "web_resources"
RESOURCES_DIR.mkdir(parents=True, exist_ok=True)
FINDINGS_FILE = DATA_DIR / "web_findings.json"


# ═══════════════════════════════════════════════════════════════════════════════
# Resource Hunter  (constant web mining for decompilation data)
# ═══════════════════════════════════════════════════════════════════════════════

class ResourceHunter:
    """
    Continuously mines the web for anything that could help match ACCF functions:
    symbol names, SDK headers, related game decomps, struct layouts, etc.

    Results are saved to data/web_resources/ and merged into data/symbol_hints.json.
    """

    # ── Curated search queries ─────────────────────────────────────────────────
    # These are ordered: most likely to find direct symbol data first.
    QUERIES: list[tuple[str, str]] = [
        # Direct ACCF resources
        ("accf_direct",      "animal crossing city folk RUUE01 decompilation symbols github"),
        ("accf_direct2",     "\"animal crossing city folk\" wii function names symbols decomp"),
        ("acgc_symbols",     "animal crossing gamecube decompilation symbols map github"),
        ("acww_symbols",     "animal crossing wild world nds decompilation symbols"),
        ("accf_github",      "site:github.com animal crossing city folk decomp"),
        # Nintendo middleware most likely in ACCF
        ("egg_library",      "EGG library nintendo GameCube source code symbols functions"),
        ("egg_github",       "site:github.com EGG nintendo GameCube library"),
        ("jsystem",          "JSystem nintendo GameCube Wii library functions source"),
        ("nw4r",             "nw4r library wii symbols function names"),
        ("rvl_sdk",          "RVL SDK wii function names headers"),
        ("dwc",              "DWC WiiConnect24 nintendo wifi functions symbols"),
        ("g3d",              "G3D nintendo 3D graphics library GameCube symbols"),
        # Related decomps that share code with ACCF (same EAD team / same engine)
        ("mk_wii",           "mario kart wii decompilation function symbols github"),
        ("tp_decomp",        "twilight princess decompilation symbols github"),
        ("smg_decomp",       "super mario galaxy decompilation symbols source"),
        ("wii_sports",       "wii sports decompilation symbols github"),
        ("pikmin",           "pikmin 2 gamecube decompilation symbols"),
        ("paper_mario",      "paper mario thousand year door decompilation symbols"),
        # Decomp.me — has crowdsourced decomp data
        ("decompme_ac",      "decomp.me animal crossing wii functions"),
        ("decompme_gc",      "site:decomp.me animal crossing GameCube"),
        # Symbol databases and tools
        ("wiibrew_sym",      "wiibrew.org wii function symbols database"),
        ("cwcc_symbols",     "mwcceppc CodeWarrior library symbols PowerPC runtime"),
        ("ppc_sdk",          "PowerPC GameCube symbol map .map file functions"),
        ("gc_map_files",     "GameCube .map symbol file nintendo decomp github"),
        # Nintendo EAD specific
        ("ead_engine",       "nintendo EAD game engine functions C++ symbols"),
        ("ead_animal",       "nintendo EAD animal crossing engine structs functions"),
        # Runtime / string functions present in 0x8045xxxx range
        ("msl_runtime",      "MetroWerks MSL runtime PowerPC function symbols"),
        ("mwcc_runtime",     "CodeWarrior runtime library functions printf sprintf"),
        # Broader sweep
        ("gc_decomps",       "site:github.com GameCube Wii decompilation symbols .map"),
        ("gc_modding",       "GameCube Wii modding function addresses animal crossing"),
        ("accf_hacking",     "animal crossing city folk wii hacking gecko codes functions"),
        ("accf_ram",         "animal crossing city folk RAM map addresses symbols"),
    ]

    # GitHub repos known to be worth fetching for symbol data
    GITHUB_TARGETS: list[str] = [
        # ACreTeam — the actual ACGC decompilation project (same codebase lineage as ACCF)
        "https://api.github.com/repos/ACreTeam/ac-decomp/git/trees/master?recursive=1",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/README.md",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/main.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/types.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/src/main.c",
        # Zelda: Twilight Princess (shares EGG/JSystem) — correct paths
        "https://raw.githubusercontent.com/zeldaret/tp/main/include/f_op/f_op_actor.h",
        "https://raw.githubusercontent.com/zeldaret/tp/main/include/m_Do/m_Do_ext.h",
        # GitHub API searches (search/code requires auth — excluded)
        "https://api.github.com/search/repositories?q=animal+crossing+decomp+gamecube&sort=stars",
        "https://api.github.com/search/repositories?q=EGG+nintendo+GameCube+decomp&sort=stars",
        # ac-decomp include tree for header names
        "https://api.github.com/repos/ACreTeam/ac-decomp/contents/include",
        "https://api.github.com/repos/ACreTeam/ac-decomp/contents/src",
    ]

    # Additional symbol sources: more ac-decomp headers + broad Nintendo ecosystem
    DECOMPME_TARGETS: list[str] = [
        # ── ac-decomp: more headers ────────────────────────────────────────────
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_common_data.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_play.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_play_h.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_lib.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_olib.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_debug.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_map_ovl.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_quest.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_kabu_manager.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_time.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/lb_rtc.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_bgm.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_actor_dlftbls.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_scene_table.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_event_map_npc.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_name_table.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_mail.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_item_name.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_field_make.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_private.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_npc_walk.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_npc_schedule.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_npc_personal_id.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_personal_id.h",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/include/m_bg_item.h",
        # ac-decomp source files (actual C code with function bodies)
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/src/PreRender.c",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/src/audio.c",
        "https://raw.githubusercontent.com/ACreTeam/ac-decomp/master/src/c_keyframe.c",
        # ── decomp.me: Cloudflare-protected, use archive.org cached API responses
        "https://web.archive.org/web/2024*/https://decomp.me/api/scratch/?platform=wii",
        "https://web.archive.org/web/2024*/https://decomp.me/api/scratch/?platform=gc_us",
        # ── decomp.dev: open tracker, no protection
        "https://decomp.dev/ACreTeam/ac-decomp",
        # ── Zelda Wind Waker (heavy EGG/JSystem overlap with AC)
        "https://raw.githubusercontent.com/zeldaret/tww/main/include/f_op/f_op_actor.h",
        "https://raw.githubusercontent.com/zeldaret/tww/main/include/f_op/f_op_actor_mng.h",
        "https://raw.githubusercontent.com/zeldaret/tww/main/include/f_op/f_op_camera.h",
        # ── Zelda TP (also shares EGG/JSystem)
        "https://raw.githubusercontent.com/zeldaret/tp/main/include/SSystem/SComponent/c_lib.h",
        "https://raw.githubusercontent.com/zeldaret/tp/main/include/d/d_com_inf_game.h",
        # ── Wiibrew wiki (no Cloudflare, raw symbol lists)
        "https://raw.githubusercontent.com/dolphin-emu/dolphin/master/Source/Core/Core/PowerPC/PPCSymbolDB.cpp",
        # ── RomFS / GameCube modding resources
        "https://raw.githubusercontent.com/simonlindholm/decomp-permuter/master/src/main.py",
    ]

    # TTLs (seconds) for re-visiting different source types
    _TTL_QUERY    = 6 * 3600   # DDG search queries: re-run every 6 hours
    _TTL_RAW_FILE = 24 * 3600  # Raw file fetches: re-fetch daily
    _TTL_404      = 7 * 86400  # 404s: retry after a week
    _TTL_HTML     = 12 * 3600  # HTML pages: re-scrape every 12 hours

    # Max raw headers to fetch per hunt_once() cycle from the dynamic queue
    _DRAIN_PER_CYCLE = 25

    def __init__(self) -> None:
        self._visited: dict[str, float] = {}  # key → last_visited timestamp
        self._lock = threading.Lock()
        self._hints_added: int = 0
        self._finds: list[dict] = self._load_finds()
        # Dynamic queue populated from the ac-decomp git tree — raw .h URLs
        # not yet in GITHUB_TARGETS or DECOMPME_TARGETS
        self._dynamic_queue: list[str] = []
        self._dynamic_known: set[str] = set(
            url for url in self.GITHUB_TARGETS + self.DECOMPME_TARGETS
            if "raw.githubusercontent.com" in url
        )

    def _ttl_for(self, key: str) -> float:
        """Return appropriate TTL for a given key."""
        if key.startswith("http"):
            if "raw.githubusercontent.com" in key or "raw.github" in key:
                return self._TTL_RAW_FILE
            if any(k in key for k in ("404", "error")):
                return self._TTL_404
            return self._TTL_HTML
        return self._TTL_QUERY  # DDG query key

    def _is_fresh(self, key: str) -> bool:
        """True if key was visited recently enough to skip."""
        last = self._visited.get(key, 0.0)
        return (time.time() - last) < self._ttl_for(key)

    def _mark_visited(self, key: str) -> None:
        self._visited[key] = time.time()

    def _load_finds(self) -> list[dict]:
        if FINDINGS_FILE.exists():
            try:
                return json.loads(FINDINGS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    def _save_finds(self) -> None:
        try:
            import tempfile as _tf, os as _os
            with _tf.NamedTemporaryFile(mode="w", suffix=".json",
                                        delete=False, encoding="utf-8") as _f:
                json.dump(self._finds[-500:], _f, indent=2)
                _ftmp = _f.name
            shutil.copy(_ftmp, str(FINDINGS_FILE))
            try: _os.unlink(_ftmp)
            except Exception: pass
        except Exception:
            pass

    # ── HTTP fetch (stdlib only) ───────────────────────────────────────────────

    def _fetch(self, url: str, timeout: int = 12) -> str:
        import urllib.request
        if self._is_fresh(url):
            return ""
        self._mark_visited(url)
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; accf-healer/1.0)",
                    "Accept": "text/html,application/json,*/*",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                # Use larger cap for GitHub API, smaller for HTML pages
                cap = 1048576 if "api.github.com" in url else 262144
                raw = r.read(cap)
                return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            _log(f"[ResourceHunter] fetch failed {url[:60]}: {exc}")
            # Mark 404s with extra-long TTL (don't hammer dead URLs)
            if "404" in str(exc) or "Not Found" in str(exc):
                self._visited[url] = time.time() + self._TTL_404
            return ""

    def _ddg_search(self, query: str, max_results: int = 8) -> list[str]:
        """DuckDuckGo HTML search → list of result URLs."""
        import urllib.request, urllib.parse
        try:
            params = urllib.parse.urlencode({"q": query, "b": ""})
            req = urllib.request.Request(
                "https://html.duckduckgo.com/html/",
                data=params.encode(),
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; accf-healer/1.0)",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=12) as r:
                html = r.read(131072).decode("utf-8", errors="replace")
            urls = re.findall(r'class="result__url"[^>]*>(.*?)</a>', html, re.DOTALL)
            snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)
            clean_urls = []
            for u in urls[:max_results]:
                u = re.sub(r"<[^>]+>", "", u).strip()
                if u and not u.startswith("http"):
                    u = "https://" + u
                if u:
                    clean_urls.append(u)
            return clean_urls
        except Exception as exc:
            _log(f"[ResourceHunter] DDG search failed: {exc}")
            return []

    # ── Symbol extraction ─────────────────────────────────────────────────────

    def _extract_symbols(self, text: str, source: str) -> list[dict]:
        """
        Pull C/C++ symbol names from source code.
        Handles: typedefs, enums, #defines, function decls, struct names,
                 Nintendo mixed-prefix conventions (mNpc_, acXxx, EGG::, etc.)
        """
        if not text:
            return []
        # Hard-reject HTML
        stripped = text.lstrip()
        if stripped.startswith(("<!DOCTYPE","<html","<!doctype","<HTML")):
            return []
        if text.count("</") > len(text) / 200:
            return []

        C_SKIP = frozenset({
            "void","int","char","bool","float","double","long","short","unsigned",
            "signed","return","const","static","inline","struct","class","union",
            "enum","typedef","else","true","false","NULL","nullptr","define",
            "include","ifndef","ifdef","endif","pragma","extern","register",
            "volatile","sizeof","typeof","auto","switch","case","break","continue",
            "default","while","for","goto","if","do","template","namespace",
            "public","private","protected","virtual","override","new","delete",
            "this","operator","using","explicit","friend","mutable",
        })

        found = []
        seen: set[str] = set()

        def add(name: str) -> None:
            name = name.strip()
            if not name or name in seen or len(name) < 3 or len(name) > 100:
                return
            if name in C_SKIP:
                return
            if name.startswith(("auto_", "_Z", "__")):
                return
            if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
                return
            # reject pure-lowercase short tokens (likely English words / HTML attrs)
            if name.islower() and '_' not in name and len(name) <= 12:
                return
            seen.add(name)
            found.append({"name": name, "source": source})

        # 1. typedef closing name: }  TypeName_c;
        for m in re.finditer(r'\}\s*([A-Za-z_][A-Za-z0-9_]{3,})\s*;', text):
            add(m.group(1))

        # 2. enum values (comma/newline-separated identifiers inside enum blocks)
        for blk in re.finditer(r'enum\s*(?:\w+\s*)?\{([^}]+)\}', text, re.DOTALL):
            for tok in re.finditer(r'\b([A-Za-z_][A-Za-z0-9_]{3,})\b', blk.group(1)):
                add(tok.group(1))

        # 3. #define NAME  or  #define NAME(
        for m in re.finditer(r'^\s*#\s*define\s+([A-Za-z_][A-Za-z0-9_]{3,})', text, re.MULTILINE):
            add(m.group(1))

        # 4. struct/class/union/enum tag names
        for m in re.finditer(r'\b(?:struct|class|union|enum)\s+([A-Za-z_][A-Za-z0-9_]{3,})', text):
            add(m.group(1))

        # 5. Function declarations / calls: Name(
        for m in re.finditer(r'\b([A-Za-z_][A-Za-z0-9_]{3,})\s*\(', text):
            n = m.group(1)
            if n not in C_SKIP:
                add(n)

        # 6. Nintendo mixed-prefix naming: mNpc_*, mActor_*, acXxx*, bgXxx*, efXxx*, evXxx*
        for m in re.finditer(
            r'\b(m[A-Z][A-Za-z0-9_]{3,}|ac[A-Z_][A-Za-z0-9_]{2,}|'
            r'bg[A-Z_][A-Za-z0-9_]{2,}|ef[A-Z_][A-Za-z0-9_]{2,}|'
            r'ev[A-Z_][A-Za-z0-9_]{2,}|lb[A-Z_][A-Za-z0-9_]{2,})', text):
            add(m.group(1))

        # 7. .map / symbol table entries: 80XXXXXX [size] symbolName
        for m in re.finditer(r'8[0-9a-fA-F]{7}\s+\S+\s+([A-Za-z_][A-Za-z0-9_]{4,})', text):
            add(m.group(1))
        for m in re.finditer(r'8[0-9a-fA-F]{7}\s+([A-Za-z_][A-Za-z0-9_]{4,})', text):
            add(m.group(1))

        # 8. Nintendo middleware namespaces: EGG::, JSystem::, nw4r::, etc.
        for m in re.finditer(
            r'\b((?:EGG|JSystem|nw4r|DWC|G3D|SSystem|TSystem|JASystem|JAudio|'
            r'JMessage|JKernel|JGadget|JStudio|mDoLib|dLib|dCom|dActor|dBgS|'
            r'fBase|fLiMgr|fManager|cLib|cM3d|lyt|math)[A-Za-z0-9_:]{2,})', text):
            add(m.group(1))

        return found

    def _merge_hints(self, symbols: list[dict]) -> int:
        """Add new symbols into data/symbol_hints.json. Returns count added."""
        if not symbols:
            return 0
        hints_file = PROJECT_ROOT / "data" / "symbol_hints.json"
        try:
            hints: dict = {}
            if hints_file.exists():
                hints = json.loads(hints_file.read_text(encoding="utf-8"))
            added = 0
            for s in symbols:
                name = s["name"]
                src  = s.get("source", "web")
                if name not in hints:
                    hints[name] = {"source": src, "confidence": 0.3}
                    added += 1
            if added:
                import tempfile as _tf, os as _os
                with _tf.NamedTemporaryFile(mode="w", suffix=".json",
                                            delete=False, encoding="utf-8") as _f:
                    json.dump(hints, _f, indent=2)
                    _ftmp = _f.name
                shutil.copy(_ftmp, str(hints_file))
                try: _os.unlink(_ftmp)
                except Exception: pass
            return added
        except Exception as exc:
            _log(f"[ResourceHunter] merge_hints error: {exc}")
            return 0

    def _save_resource(self, name: str, content: str, symbols: list[dict]) -> None:
        """Save a found resource to data/web_resources/."""
        try:
            out = RESOURCES_DIR / f"{name}.json"
            data = {
                "ts":      time.time(),
                "content": content[:8000],
                "symbols": symbols[:200],
            }
            import tempfile as _tf, os as _os
            with _tf.NamedTemporaryFile(mode="w", suffix=".json",
                                        delete=False, encoding="utf-8") as _f:
                json.dump(data, _f, indent=2)
                _ftmp = _f.name
            shutil.copy(_ftmp, str(out))
            try: _os.unlink(_ftmp)
            except Exception: pass
        except Exception:
            pass

    # ── Main hunt loop ────────────────────────────────────────────────────────

    def hunt_once(self, extra_queries: list[str] | None = None) -> int:
        """
        Run one pass of the resource hunt.
        Returns total number of new symbols found.
        """
        total_new = 0
        queries_to_run = list(self.QUERIES)
        if extra_queries:
            for eq in extra_queries:
                queries_to_run.append(("dynamic_" + re.sub(r'\W+', '_', eq)[:30], eq))

        for qkey, query in queries_to_run:
            if self._is_fresh(qkey):
                continue
            self._mark_visited(qkey)

            _log(f"[ResourceHunter] searching: {query[:60]}")
            urls = self._ddg_search(query, max_results=5)

            for url in urls:
                # prioritise GitHub raw/API and known decomp sites
                priority = any(d in url for d in (
                    "github.com", "decomp.me", "wiibrew.org", "decomp.dev",
                    "raw.githubusercontent.com", "pastebin.com",
                    "web.archive.org", "gist.github.com", "romhacking.net",
                    "gbatemp.net", "tcrf.net", "kuribo64.net",
                ))
                if not priority:
                    continue  # skip random blog posts
                content = self._fetch(url)
                if not content:
                    continue
                syms = self._extract_symbols(content, url)
                if syms:
                    added = self._merge_hints(syms)
                    if added:
                        total_new += added
                        slug = re.sub(r'\W+', '_', url)[:40]
                        self._save_resource(f"{qkey}_{slug}", content, syms)
                        self._finds.append({
                            "ts": time.time(), "url": url,
                            "query": query, "symbols_added": added,
                        })
                        _log(f"[ResourceHunter] +{added} symbols from {url[:60]}")
                time.sleep(0.3)  # be polite

        # Also probe curated GitHub/decomp.me targets
        for url in self.GITHUB_TARGETS + self.DECOMPME_TARGETS:
            content = self._fetch(url)
            if not content:
                continue
            # GitHub API tree → extract all .c/.h/.cpp filenames as hints
            # AND populate _dynamic_queue with raw URLs for full extraction
            if "api.github.com" in url and "trees" in url:
                try:
                    # Response may be truncated — extract paths via regex as fallback
                    paths = re.findall(r'"path"\s*:\s*"([^"]+)"', content)
                    file_hints = []
                    enqueued = 0
                    for fpath in paths:
                        if fpath.endswith((".c", ".h", ".cpp", ".hpp")):
                            stem = Path(fpath).stem
                            if len(stem) > 3 and not stem.startswith("auto_"):
                                file_hints.append({"name": stem, "source": "ac-decomp-tree"})
                            # Build raw URL and enqueue if not already known/visited
                            raw_url = (
                                f"https://raw.githubusercontent.com/ACreTeam/ac-decomp"
                                f"/master/{fpath}"
                            )
                            if (raw_url not in self._dynamic_known
                                    and not self._is_fresh(raw_url)
                                    and raw_url not in self._dynamic_queue):
                                self._dynamic_queue.append(raw_url)
                                self._dynamic_known.add(raw_url)
                                enqueued += 1
                    if file_hints:
                        added = self._merge_hints(file_hints)
                        total_new += added
                        _log(
                            f"[ResourceHunter] ac-decomp tree: +{added} hints "
                            f"from {len(paths)} paths; queued {enqueued} new raw URLs "
                            f"(queue depth={len(self._dynamic_queue)})"
                        )
                    # Also try full JSON parse for structured data
                    try:
                        data = json.loads(content)
                        tree = data.get("tree", [])
                        for item in tree:
                            fpath = item.get("path", "")
                            if fpath.endswith((".c",".h",".cpp",".hpp")):
                                stem = Path(fpath).stem
                                if stem not in {h["name"] for h in file_hints}:
                                    file_hints.append({"name": stem, "source": "ac-decomp-tree"})
                                raw_url = (
                                    f"https://raw.githubusercontent.com/ACreTeam/ac-decomp"
                                    f"/master/{fpath}"
                                )
                                if (raw_url not in self._dynamic_known
                                        and not self._is_fresh(raw_url)
                                        and raw_url not in self._dynamic_queue):
                                    self._dynamic_queue.append(raw_url)
                                    self._dynamic_known.add(raw_url)
                    except Exception:
                        pass  # truncated JSON — regex result is fine
                except Exception:
                    pass
            # GitHub API search results → extract repo names and descriptions
            elif "api.github.com/search" in url:
                try:
                    data = json.loads(content)
                    items = data.get("items", []) or data.get("files", [])
                    for item in items[:10]:
                        name = item.get("name", item.get("full_name", ""))
                        desc = item.get("description", "")
                        for candidate in [name, desc]:
                            syms = self._extract_symbols(candidate or "", url)
                            if syms:
                                self._merge_hints(syms)
                except Exception:
                    pass
            # GitHub API directory listing → recurse one level
            elif "api.github.com/repos" in url and "/contents/" in url:
                try:
                    items = json.loads(content)
                    if isinstance(items, list):
                        for item in items:
                            if item.get("type") == "file":
                                raw_url = item.get("download_url", "")
                                if raw_url and any(raw_url.endswith(x)
                                                   for x in (".h",".c",".cpp",".hpp")):
                                    sub = self._fetch(raw_url)
                                    if sub:
                                        syms = self._extract_symbols(sub, raw_url)
                                        if syms:
                                            added = self._merge_hints(syms)
                                            total_new += added
                                            if added:
                                                _log(f"[ResourceHunter] +{added} from {raw_url[-50:]}")
                except Exception:
                    pass
            else:
                syms = self._extract_symbols(content, url)
                if syms:
                    added = self._merge_hints(syms)
                    total_new += added
                    if added:
                        slug = re.sub(r'\W+', '_', url)[:40]
                        self._save_resource(f"curated_{slug}", content, syms)
                        _log(f"[ResourceHunter] +{added} symbols from curated {url[:60]}")

        # ── Drain dynamic queue: fetch up to _DRAIN_PER_CYCLE raw headers
        #    discovered from the ac-decomp git tree (509+ untapped files)
        drained = 0
        while self._dynamic_queue and drained < self._DRAIN_PER_CYCLE:
            raw_url = self._dynamic_queue.pop(0)
            if self._is_fresh(raw_url):
                continue  # already fetched this cycle or recently
            content = self._fetch(raw_url)
            if not content:
                continue
            syms = self._extract_symbols(content, raw_url)
            if syms:
                added = self._merge_hints(syms)
                total_new += added
                if added:
                    slug = re.sub(r'\W+', '_', raw_url)[:40]
                    self._save_resource(f"dynamic_{slug}", content, syms)
                    self._finds.append({
                        "ts": time.time(), "url": raw_url,
                        "query": "dynamic-tree-queue", "symbols_added": added,
                    })
                    _log(f"[ResourceHunter] +{added} symbols from dynamic {raw_url.split('/')[-1]}")
            drained += 1
            time.sleep(0.15)  # polite GitHub rate-limiting

        if drained:
            _log(
                f"[ResourceHunter] drained {drained} from dynamic queue "
                f"({len(self._dynamic_queue)} remaining)"
            )

        self._save_finds()
        return total_new

    def hunt_for_address(self, addr_hex: str) -> int:
        """Targeted hunt for a specific function address."""
        queries = [
            f"animal crossing wii function {addr_hex} symbol name",
            f"ACCF {addr_hex} function decomp",
            f"wii {addr_hex} disassembly symbol",
        ]
        total = 0
        for q in queries:
            urls = self._ddg_search(q, max_results=3)
            for url in urls:
                content = self._fetch(url)
                if content:
                    syms = self._extract_symbols(content, url)
                    total += self._merge_hints(syms)
            time.sleep(0.2)
        return total


# ═══════════════════════════════════════════════════════════════════════════════
# Proactive Analyzer  (periodic analysis + visible status printing)
# ═══════════════════════════════════════════════════════════════════════════════

class ProactiveAnalyzer:
    """
    Runs every INTERVAL seconds.
    - Reads build/RUUE01/report.json to see which units are stuck.
    - Groups stuck addresses by subsystem range.
    - Triggers ResourceHunter with targeted queries.
    - Prints visible status to stdout.
    - Looks at logs for patterns and suggests improvements.
    """
    INTERVAL   = 300   # 5 minutes
    HUNT_EVERY = 2     # run a full hunt every Nth analysis cycle

    # Known ACCF address ranges → subsystem names
    SUBSYSTEMS: list[tuple[int, int, str]] = [
        (0x80000000, 0x80100000, "Bootstrap/OS"),
        (0x80100000, 0x80200000, "EGG/JSystem core"),
        (0x80200000, 0x80300000, "EGG/AC engine A"),
        (0x80300000, 0x80400000, "AC engine B"),
        (0x80400000, 0x80440000, "AC game logic"),
        (0x80440000, 0x80450000, "AC game logic (high)"),
        (0x80450000, 0x80460000, "MSL/CW runtime"),
    ]

    def __init__(self, hunter: "ResourceHunter", metrics: "MetricsTracker") -> None:
        self._hunter  = hunter
        self._metrics = metrics
        self._cycle   = 0
        self._thread  = threading.Thread(
            target=self._loop, daemon=True, name="proactive-analyzer"
        )
        self._thread.start()

    def _loop(self) -> None:
        time.sleep(30)  # let the main workers get started first
        while True:
            try:
                self._analyze()
            except Exception as exc:
                _log(f"[ProactiveAnalyzer] exception: {exc}")
            time.sleep(self.INTERVAL)

    def _subsystem(self, addr: int) -> str:
        for lo, hi, name in self.SUBSYSTEMS:
            if lo <= addr < hi:
                return name
        return "Unknown"

    def _read_report(self) -> tuple[int, int, dict[str, list[str]]]:
        """
        Returns (total, stuck_count, subsystem_buckets).
        subsystem_buckets maps subsystem name → list of stuck unit short names.
        """
        report_file = PROJECT_ROOT / "build" / "RUUE01" / "report.json"
        if not report_file.exists():
            return 0, 0, {}
        try:
            data = json.loads(report_file.read_text(encoding="utf-8"))
        except Exception:
            return 0, 0, {}
        units = data.get("units", [])
        total  = len(units)
        buckets: dict[str, list[str]] = {}
        stuck  = 0
        for u in units:
            pct = u.get("measures", {}).get("fuzzy_match_percent", 0.0)
            if pct < 1.0:
                stuck += 1
                name = u.get("name", "")
                # Extract hex address from name like auto_fn_80451234_text
                m = re.search(r'([89][0-9a-fA-F]{7})', name)
                if m:
                    addr = int(m.group(1), 16)
                    sub  = self._subsystem(addr)
                else:
                    sub = "Unknown"
                buckets.setdefault(sub, []).append(name)
        return total, stuck, buckets

    def _analyze(self) -> None:
        self._cycle += 1
        total, stuck, buckets = self._read_report()
        done    = total - stuck
        avg_pct = self._metrics.recent_avg_match(20)
        stag    = self._metrics.stagnant()

        # ── Status line (always visible in autopilot output) ──────────────────
        bar = "█" * min(20, int(done / max(total, 1) * 20))
        bar += "░" * (20 - len(bar))
        status_parts = [f"  [Healer] {done}/{total} done [{bar}]  avg={avg_pct:.1f}%"]
        if stag:
            status_parts.append(" STAGNANT")
        if buckets:
            top_sub = max(buckets, key=lambda k: len(buckets[k]))
            status_parts.append(f"  heaviest: {top_sub} ({len(buckets[top_sub])} stuck)")
        print("".join(status_parts), flush=True)

        # ── Subsystem breakdown every 3rd cycle ───────────────────────────────
        if self._cycle % 3 == 0 and buckets:
            print("  [Healer] Subsystem breakdown:", flush=True)
            for sub, names in sorted(buckets.items(), key=lambda x: -len(x[1])):
                print(f"    {sub:<30}  {len(names):>4} stuck", flush=True)

        # ── Resource hunt ─────────────────────────────────────────────────────
        if self._cycle % self.HUNT_EVERY == 0:
            extra: list[str] = []
            # Build targeted queries from biggest stuck subsystems
            for sub, names in sorted(buckets.items(), key=lambda x: -len(x[1]))[:3]:
                extra.append(f"nintendo {sub} functions symbols GameCube Wii")
            # If stagnant, do an address-targeted search on the top stuck unit
            if stag and buckets:
                top_names = list(buckets.values())[0]
                if top_names:
                    m = re.search(r'([89][0-9a-fA-F]{7})', top_names[0])
                    if m:
                        extra.append(f"animal crossing wii 0x{m.group(1)} function symbol")

            print(f"  [Healer] Hunting resources ({len(extra)} targeted queries) ...", flush=True)
            found = self._hunter.hunt_once(extra_queries=extra or None)
            if found:
                print(f"  [Healer] +{found} new symbols added to symbol_hints.json", flush=True)
            else:
                print(f"  [Healer] No new symbols this pass (all known sources checked)", flush=True)

        # ── File integrity check — catch CIFS truncation before it causes pain ──
        self._check_file_integrity()

        # ── Log pattern scan — look for repeated errors ────────────────────────
        self._scan_logs()

    # ── CIFS truncation signatures ─────────────────────────────────────────────
    # Each entry: (file relative to PROJECT_ROOT, required_ending_bytes, fix_label)
    # required_ending_bytes: the file MUST end with this string (stripped).
    # If it doesn't, we restore from the snapshot stored in data/file_snapshots/.
    _INTEGRITY_CHECKS: list[tuple[str, str]] = [
        ("tools/autopilot.py",     '    main()'),
        ("tools/autopilot_win.ps1", "python tools\\autopilot.py @args"),
        ("tools/ida_server.py",    "ida_kernwin.register_timer(100, _drain_queue)"),
        ("tools/self_healer.py",   "# end of self_healer"),
        ("tools/decomp_loop.py",   '    main()'),
    ]
    _SNAPSHOTS_DIR = DATA_DIR / "file_snapshots"

    def _snapshot_path(self, rel: str) -> Path:
        return self._SNAPSHOTS_DIR / rel.replace("/", "__").replace("\\", "__")

    def _save_snapshot(self, rel: str, content: bytes) -> None:
        self._SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        dest = self._snapshot_path(rel)
        try:
            import tempfile as _tf, shutil as _sh
            with _tf.NamedTemporaryFile(delete=False, suffix=".snap") as _f:
                _f.write(content)
                _tmp = _f.name
            _sh.copy(_tmp, str(dest))
            import os as _os; _os.unlink(_tmp)
        except Exception as exc:
            _log(f"[FileIntegrity] snapshot write failed for {rel}: {exc}")

    def _check_file_integrity(self) -> None:
        """
        Detect CIFS-truncated source files and restore from snapshot.
        On each cycle:
          1. For every watched file: if the content ends correctly → update snapshot.
          2. If it's truncated → restore from snapshot and alert loudly.
        """
        for rel, required_ending in self._INTEGRITY_CHECKS:
            fpath = PROJECT_ROOT / rel
            if not fpath.exists():
                continue
            try:
                content = fpath.read_bytes()
            except Exception:
                continue

            text_tail = content.rstrip(b"\r\n \t")
            # Check if file ends with the required sentinel
            ok = text_tail.endswith(required_ending.encode("utf-8"))

            if ok:
                # Healthy — refresh snapshot
                snap = self._snapshot_path(rel)
                if not snap.exists() or snap.stat().st_size != len(content):
                    self._save_snapshot(rel, content)
            else:
                # Truncated — try to restore
                snap = self._snapshot_path(rel)
                print(
                    f"\n  [Healer] !! TRUNCATED FILE DETECTED: {rel}\n"
                    f"           Last bytes: {repr(content[-60:])}\n"
                    f"           Expected ending: {repr(required_ending[-40:])}",
                    flush=True,
                )
                _log(f"[FileIntegrity] truncated: {rel} last={repr(content[-60:])}")
                if snap.exists():
                    try:
                        import shutil as _sh, tempfile as _tf
                        snap_content = snap.read_bytes()
                        with _tf.NamedTemporaryFile(
                            delete=False, suffix=".tmp",
                            dir=str(fpath.parent)
                        ) as _f:
                            _f.write(snap_content)
                            _tmp = _f.name
                        _sh.copy(_tmp, str(fpath))
                        import os as _os; _os.unlink(_tmp)
                        print(
                            f"  [Healer] ✔  Restored {rel} from snapshot "
                            f"({len(snap_content)} bytes)",
                            flush=True,
                        )
                    except Exception as exc:
                        print(f"  [Healer] !! Restore failed for {rel}: {exc}", flush=True)
                else:
                    print(
                        f"  [Healer] !! No snapshot yet for {rel} — "
                        "will save one next healthy cycle.",
                        flush=True,
                    )

    def _scan_logs(self) -> None:
        """Scan recent log files for repeated error patterns and report them."""
        error_counts: dict[str, int] = {}
        credit_exhausted = False
        ollama_errors    = 0
        for logf in LOGS_DIR.glob("*.log"):
            try:
                txt = logf.read_text(encoding="utf-8", errors="replace")[-8000:]
                # Specific high-priority checks
                if "credit balance" in txt.lower() or "credits exhausted" in txt.lower():
                    credit_exhausted = True
                if "ollama" in logf.name.lower() or "connection refused" in txt:
                    ollama_errors += txt.lower().count("connection refused")
                # count error signatures
                for m in re.finditer(r'(Error|error|failed|crash|timeout|exception)', txt):
                    snippet = txt[max(0, m.start()-20):m.start()+60].replace("\n", " ")
                    key = re.sub(r'\d+', 'N', snippet)[:60]
                    error_counts[key] = error_counts.get(key, 0) + 1
            except Exception:
                pass

        if credit_exhausted:
            print("  [Healer] !! CLAUDE API CREDITS EXHAUSTED -- top up at console.anthropic.com", flush=True)
            _log("[ProactiveAnalyzer] credit balance exhausted")

        if ollama_errors > 10:
            print(f"  [Healer] !! Ollama connection errors ({ollama_errors}x) -- is Ollama still running?", flush=True)

        hot = [(v, k) for k, v in error_counts.items() if v >= 5]
        hot.sort(reverse=True)
        for count, pattern in hot[:2]:
            if "credit" in pattern.lower():
                continue
            print(f"  [Healer] Recurring ({count}x): {pattern[:80]}", flush=True)


class SelfHealingEngine:
    def __init__(self) -> None:
        self._kb       = KnowledgeBase()
        self._metrics  = MetricsTracker()
        self._hunter   = ResourceHunter()
        self._analyzer = ProactiveAnalyzer(self._hunter, self._metrics)
        _log("[SelfHealingEngine] started (with ProactiveAnalyzer + ResourceHunter)")
        print("[SelfHealingEngine] started (with ProactiveAnalyzer + ResourceHunter)", flush=True)

    def report_incident(self, incident_type: str, context: dict) -> None:
        inc = Incident(
            type=IncidentType(incident_type)
            if incident_type in [e.value for e in IncidentType]
            else IncidentType.GENERIC,
            context=context,
        )
        ActionExecutor(self._kb).execute(inc)

    def record_match(self, unit: str, pct: float) -> None:
        self._metrics.record_match(unit, pct)

    def status(self) -> dict:
        finds = self._hunter._finds
        last_ts = finds[-1]["ts"] if finds else None
        return {
            "avg_match":   self._metrics.recent_avg_match(20),
            "kb_entries":  len(self._kb._entries),
            "web_finds":   len(finds),
            "last_find":   last_ts,
            "queue_depth": len(self._hunter._dynamic_queue),
        }


_engine: "SelfHealingEngine | None" = None

def _get_engine() -> "SelfHealingEngine":
    global _engine
    if _engine is None:
        _engine = SelfHealingEngine()
    return _engine


def report_incident(incident_type: str, context: dict | None = None) -> None:
    try:
        _get_engine().report_incident(incident_type, context or {})
    except Exception as exc:
        _log(f"[report_incident] error: {exc}")


def record_match(unit: str, pct: float) -> None:
    try:
        _get_engine().record_match(unit, pct)
    except Exception as exc:
        _log(f"[record_match] error: {exc}")


if __name__ == "__main__":
    import sys as _sys
    cmd = _sys.argv[1] if len(_sys.argv) > 1 else "status"
    if cmd == "status":
        e = _get_engine()
        s = e.status()
        print(f"Average match rate (last 20): {s['avg_match']:.1f}%")
        print(f"KB entries: {s['kb_entries']}")
        print(f"Web finds: {s['web_finds']} resources, last: {time.strftime('%H:%M:%S', time.localtime(s['last_find'])) if s['last_find'] else 'none'}")
        print(f"Dynamic queue depth: {s['queue_depth']}")
        print(f"Metrics: {METRICS_FILE}")
        print(f"Log:     {LOG_FILE}")
    elif cmd == "hunt":
        print("Running hunt_once() ...")
        n = _get_engine()._hunter.hunt_once()
        print(f"hunt_once() -> {n} new symbols")
    elif cmd == "integrity":
        print("Running file integrity check ...")
        _get_engine()._analyzer._check_file_integrity()
    else:
        print(f"Unknown command: {cmd}")

# end of self_healer
