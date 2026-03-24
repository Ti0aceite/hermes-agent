#!/usr/bin/env python3
"""
Browser Tool Module

This module provides browser automation tools using agent-browser CLI.  It
supports two backends — **Browserbase** (cloud) and **local Chromium** — with
identical agent-facing behaviour.  The backend is auto-detected: if
``BROWSERBASE_API_KEY`` is set the cloud service is used; otherwise a local
headless Chromium instance is launched automatically.

The tool uses agent-browser's accessibility tree (ariaSnapshot) for text-based
page representation, making it ideal for LLM agents without vision capabilities.

Features:
- **Local mode** (default): zero-cost headless Chromium via agent-browser.
  Works on Linux servers without a display.  One-time setup:
  ``agent-browser install`` (downloads Chromium) or
  ``agent-browser install --with-deps`` (also installs system libraries for
  Debian/Ubuntu/Docker).
- **Cloud mode**: Browserbase cloud execution with stealth features, proxies,
  and CAPTCHA solving.  Activated when BROWSERBASE_API_KEY is set.
- Session isolation per task ID
- Text-based page snapshots using accessibility tree
- Element interaction via ref selectors (@e1, @e2, etc.)
- Task-aware content extraction using LLM summarization
- Automatic cleanup of browser sessions

Environment Variables:
- BROWSERBASE_API_KEY: API key for Browserbase (enables cloud mode)
- BROWSERBASE_PROJECT_ID: Project ID for Browserbase (required for cloud mode)
- BROWSERBASE_PROXIES: Enable/disable residential proxies (default: "true")
- BROWSERBASE_ADVANCED_STEALTH: Enable advanced stealth mode with custom Chromium,
  requires Scale Plan (default: "false")
- BROWSERBASE_KEEP_ALIVE: Enable keepAlive for session reconnection after disconnects,
  requires paid plan (default: "true")
- BROWSERBASE_SESSION_TIMEOUT: Custom session timeout in milliseconds. Set to extend
  beyond project default. Common values: 600000 (10min), 1800000 (30min) (default: none)

Usage:
    from tools.browser_tool import browser_navigate, browser_snapshot, browser_click
    
    # Navigate to a page
    result = browser_navigate("https://example.com", task_id="task_123")
    
    # Get page snapshot
    snapshot = browser_snapshot(task_id="task_123")
    
    # Click an element
    browser_click("@e5", task_id="task_123")
"""

import atexit
import json
import logging
import os
import re
import signal
import subprocess
import shutil
import sys
import tempfile
import threading
import time
import requests
from typing import Dict, Any, Optional, List
from pathlib import Path
from urllib.parse import urljoin, urlparse
from agent.auxiliary_client import call_llm

try:
    from tools.website_policy import check_website_access
except Exception:
    check_website_access = lambda url: None  # noqa: E731 — fail-open if policy module unavailable
from tools.browser_providers.base import CloudBrowserProvider
from tools.browser_providers.browserbase import BrowserbaseProvider
from tools.browser_providers.browser_use import BrowserUseProvider

logger = logging.getLogger(__name__)

# Standard PATH entries for environments with minimal PATH (e.g. systemd services)
_SANE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Throttle screenshot cleanup to avoid repeated full directory scans.
_last_screenshot_cleanup_by_dir: dict[str, float] = {}

# ============================================================================
# Configuration
# ============================================================================

# Default timeout for browser commands (seconds)
DEFAULT_COMMAND_TIMEOUT = 30

# Default session timeout (seconds)
DEFAULT_SESSION_TIMEOUT = 300

# Max tokens for snapshot content before summarization
SNAPSHOT_SUMMARIZE_THRESHOLD = 8000
SNAPSHOT_STABILIZE_DELAYS = (0.0, 1.0, 2.0)
EDITABLE_SNAPSHOT_ROLE_MARKERS = ("textbox", "searchbox", "textarea", "input")

# Dependent dropdowns in apps like Dentidesk sometimes populate a moment after
# the first select fires its change event. Wait briefly before giving up.
SELECT_OPTION_WAIT_TIMEOUT = 5.0
SELECT_OPTION_POLL_INTERVAL = 0.25
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _get_vision_model() -> Optional[str]:
    """Model for browser_vision (screenshot analysis — multimodal)."""
    return os.getenv("AUXILIARY_VISION_MODEL", "").strip() or None


def _get_extraction_model() -> Optional[str]:
    """Model for page snapshot text summarization — same as web_extract."""
    return os.getenv("AUXILIARY_WEB_EXTRACT_MODEL", "").strip() or None


def _resolve_cdp_override(cdp_url: str) -> str:
    """Normalize a user-supplied CDP endpoint into a concrete connectable URL.

    Accepts:
    - full websocket endpoints: ws://host:port/devtools/browser/...
    - HTTP discovery endpoints: http://host:port or http://host:port/json/version
    - bare websocket host:port values like ws://host:port

    For discovery-style endpoints we fetch /json/version and return the
    webSocketDebuggerUrl so downstream tools always receive a concrete browser
    websocket instead of an ambiguous host:port URL.
    """
    raw = (cdp_url or "").strip()
    if not raw:
        return ""

    lowered = raw.lower()
    if "/devtools/browser/" in lowered:
        return raw

    discovery_url = raw
    if lowered.startswith("ws://") or lowered.startswith("wss://"):
        if raw.count(":") == 2 and raw.rstrip("/").rsplit(":", 1)[-1].isdigit() and "/" not in raw.split(":", 2)[-1]:
            discovery_url = ("http://" if lowered.startswith("ws://") else "https://") + raw.split("://", 1)[1]
        else:
            return raw

    if discovery_url.lower().endswith("/json/version"):
        version_url = discovery_url
    else:
        version_url = discovery_url.rstrip("/") + "/json/version"

    try:
        response = requests.get(version_url, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.warning("Failed to resolve CDP endpoint %s via %s: %s", raw, version_url, exc)
        return raw

    ws_url = str(payload.get("webSocketDebuggerUrl") or "").strip()
    if ws_url:
        logger.info("Resolved CDP endpoint %s -> %s", raw, ws_url)
        return ws_url

    logger.warning("CDP discovery at %s did not return webSocketDebuggerUrl; using raw endpoint", version_url)
    return raw


def _get_cdp_override() -> str:
    """Return a normalized user-supplied CDP URL override, or empty string.

    When ``BROWSER_CDP_URL`` is set (e.g. via ``/browser connect``), we skip
    both Browserbase and the local headless launcher and connect directly to
    the supplied Chrome DevTools Protocol endpoint.
    """
    return _resolve_cdp_override(os.environ.get("BROWSER_CDP_URL", ""))


# ============================================================================
# Cloud Provider Registry
# ============================================================================

_PROVIDER_REGISTRY: Dict[str, type] = {
    "browserbase": BrowserbaseProvider,
    "browser-use": BrowserUseProvider,
}

_cached_cloud_provider: Optional[CloudBrowserProvider] = None
_cloud_provider_resolved = False


def _get_cloud_provider() -> Optional[CloudBrowserProvider]:
    """Return the configured cloud browser provider, or None for local mode.

    Reads ``config["browser"]["cloud_provider"]`` once and caches the result
    for the process lifetime.  If unset → local mode (None).
    """
    global _cached_cloud_provider, _cloud_provider_resolved
    if _cloud_provider_resolved:
        return _cached_cloud_provider

    _cloud_provider_resolved = True
    try:
        hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        config_path = hermes_home / "config.yaml"
        if config_path.exists():
            import yaml
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            provider_key = cfg.get("browser", {}).get("cloud_provider")
            if provider_key and provider_key in _PROVIDER_REGISTRY:
                _cached_cloud_provider = _PROVIDER_REGISTRY[provider_key]()
    except Exception as e:
        logger.debug("Could not read cloud_provider from config: %s", e)
    return _cached_cloud_provider


def _socket_safe_tmpdir() -> str:
    """Return a short temp directory path suitable for Unix domain sockets.

    macOS sets ``TMPDIR`` to ``/var/folders/xx/.../T/`` (~51 chars).  When we
    append ``agent-browser-hermes_…`` the resulting socket path exceeds the
    104-byte macOS limit for ``AF_UNIX`` addresses, causing agent-browser to
    fail with "Failed to create socket directory" or silent screenshot failures.

    Linux ``tempfile.gettempdir()`` already returns ``/tmp``, so this is a
    no-op there.  On macOS we bypass ``TMPDIR`` and use ``/tmp`` directly
    (symlink to ``/private/tmp``, sticky-bit protected, always available).
    """
    if sys.platform == "darwin":
        return "/tmp"
    return tempfile.gettempdir()


# Track active sessions per task
# Stores: session_name (always), bb_session_id + cdp_url (cloud mode only)
_active_sessions: Dict[str, Dict[str, str]] = {}  # task_id -> {session_name, ...}
_recording_sessions: set = set()  # task_ids with active recordings

# Flag to track if cleanup has been done
_cleanup_done = False

# =============================================================================
# Inactivity Timeout Configuration
# =============================================================================

# Session inactivity timeout (seconds) - cleanup if no activity for this long
# Default: 5 minutes. Needs headroom for LLM reasoning between browser commands,
# especially when subagents are doing multi-step browser tasks.
BROWSER_SESSION_INACTIVITY_TIMEOUT = int(os.environ.get("BROWSER_INACTIVITY_TIMEOUT", "300"))

# Track last activity time per session
_session_last_activity: Dict[str, float] = {}

# Background cleanup thread state
_cleanup_thread = None
_cleanup_running = False
# Protects _session_last_activity AND _active_sessions for thread safety
# (subagents run concurrently via ThreadPoolExecutor)
_cleanup_lock = threading.Lock()


def _emergency_cleanup_all_sessions():
    """
    Emergency cleanup of all active browser sessions.
    Called on process exit or interrupt to prevent orphaned sessions.
    """
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True
    
    if not _active_sessions:
        return
    
    logger.info("Emergency cleanup: closing %s active session(s)...",
                len(_active_sessions))

    try:
        cleanup_all_browsers()
    except Exception as e:
        logger.error("Emergency cleanup error: %s", e)
    finally:
        with _cleanup_lock:
            _active_sessions.clear()
            _session_last_activity.clear()
        _recording_sessions.clear()


# Register cleanup via atexit only.  Previous versions installed SIGINT/SIGTERM
# handlers that called sys.exit(), but this conflicts with prompt_toolkit's
# async event loop — a SystemExit raised inside a key-binding callback
# corrupts the coroutine state and makes the process unkillable.  atexit
# handlers run on any normal exit (including sys.exit), so browser sessions
# are still cleaned up without hijacking signals.
atexit.register(_emergency_cleanup_all_sessions)


# =============================================================================
# Inactivity Cleanup Functions
# =============================================================================

def _cleanup_inactive_browser_sessions():
    """
    Clean up browser sessions that have been inactive for longer than the timeout.
    
    This function is called periodically by the background cleanup thread to
    automatically close sessions that haven't been used recently, preventing
    orphaned sessions (local or Browserbase) from accumulating.
    """
    current_time = time.time()
    sessions_to_cleanup = []
    
    with _cleanup_lock:
        for task_id, last_time in list(_session_last_activity.items()):
            if current_time - last_time > BROWSER_SESSION_INACTIVITY_TIMEOUT:
                sessions_to_cleanup.append(task_id)
    
    for task_id in sessions_to_cleanup:
        try:
            elapsed = int(current_time - _session_last_activity.get(task_id, current_time))
            logger.info("Cleaning up inactive session for task: %s (inactive for %ss)", task_id, elapsed)
            cleanup_browser(task_id)
            with _cleanup_lock:
                if task_id in _session_last_activity:
                    del _session_last_activity[task_id]
        except Exception as e:
            logger.warning("Error cleaning up inactive session %s: %s", task_id, e)


def _browser_cleanup_thread_worker():
    """
    Background thread that periodically cleans up inactive browser sessions.
    
    Runs every 30 seconds and checks for sessions that haven't been used
    within the BROWSER_SESSION_INACTIVITY_TIMEOUT period.
    """
    global _cleanup_running
    
    while _cleanup_running:
        try:
            _cleanup_inactive_browser_sessions()
        except Exception as e:
            logger.warning("Cleanup thread error: %s", e)
        
        # Sleep in 1-second intervals so we can stop quickly if needed
        for _ in range(30):
            if not _cleanup_running:
                break
            time.sleep(1)


def _start_browser_cleanup_thread():
    """Start the background cleanup thread if not already running."""
    global _cleanup_thread, _cleanup_running
    
    with _cleanup_lock:
        if _cleanup_thread is None or not _cleanup_thread.is_alive():
            _cleanup_running = True
            _cleanup_thread = threading.Thread(
                target=_browser_cleanup_thread_worker,
                daemon=True,
                name="browser-cleanup"
            )
            _cleanup_thread.start()
            logger.info("Started inactivity cleanup thread (timeout: %ss)", BROWSER_SESSION_INACTIVITY_TIMEOUT)


def _stop_browser_cleanup_thread():
    """Stop the background cleanup thread."""
    global _cleanup_running
    _cleanup_running = False
    if _cleanup_thread is not None:
        _cleanup_thread.join(timeout=5)


def _update_session_activity(task_id: str):
    """Update the last activity timestamp for a session."""
    with _cleanup_lock:
        _session_last_activity[task_id] = time.time()


# Register cleanup thread stop on exit
atexit.register(_stop_browser_cleanup_thread)


# ============================================================================
# Tool Schemas
# ============================================================================

BROWSER_TOOL_SCHEMAS = [
    {
        "name": "browser_navigate",
        "description": "Navigate to a URL in the browser. Initializes the session and loads the page. Must be called before other browser tools. For simple information retrieval, prefer web_search or web_extract (faster, cheaper). Use browser tools when you need to interact with a page (click, fill forms, dynamic content).",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to navigate to (e.g., 'https://example.com')"
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "browser_snapshot",
        "description": "Get a text-based snapshot of the current page's accessibility tree. Returns interactive elements with ref IDs (like @e1, @e2) for browser_click and browser_type. full=false (default): compact view with interactive elements. full=true: complete page content. stabilize=true retries the snapshot over a short window and returns the richest successful result, which helps with pages that finish rendering asynchronously after navigation or form submission. Snapshots over 8000 chars are truncated or LLM-summarized. Requires browser_navigate first.",
        "parameters": {
            "type": "object",
            "properties": {
                "full": {
                    "type": "boolean",
                    "description": "If true, returns complete page content. If false (default), returns compact view with interactive elements only.",
                    "default": False
                },
                "stabilize": {
                    "type": "boolean",
                    "description": "If true, retries snapshot capture over a short window and returns the richest successful result. Use this for pages that populate content asynchronously after selecting filters or clicking submit.",
                    "default": False
                }
            },
            "required": []
        }
    },
    {
        "name": "browser_click",
        "description": "Click on an element identified by its ref ID from the snapshot (e.g., '@e5'). Hermes hydrates the current snapshot before clicking and retries once if the click fails because the ref/selector is stale or invalid. For links with a navigable href, Hermes automatically treats the click as navigation and may return the new page's URL, title, and snapshot. The ref IDs are shown in square brackets in the snapshot output. Requires browser_navigate and browser_snapshot to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The element reference from the snapshot (e.g., '@e5', '@e12')"
                }
            },
            "required": ["ref"]
        }
    },
    {
        "name": "browser_select",
        "description": "Select an option in a dropdown identified by its ref ID. Provide either value (the option label/value) or option_ref (a ref to an option from the snapshot). Hermes hydrates the current snapshot before selecting and resolves option_ref from the snapshot when needed. Requires browser_navigate and browser_snapshot to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The select element reference from the snapshot (e.g., '@e5')"
                },
                "value": {
                    "type": "string",
                    "description": "The option value to select"
                },
                "option_ref": {
                    "type": "string",
                    "description": "Reference to an option element in the snapshot (e.g., '@e9')"
                }
            },
            "required": ["ref"]
        }
    },
    {
        "name": "browser_click_row_detail",
        "description": "Find a table row whose text includes row_text and click the rightmost interactive control in that row, such as a detail icon or lupa. Use this when icon-only controls are not exposed in browser_snapshot. Requires browser_navigate first.",
        "parameters": {
            "type": "object",
            "properties": {
                "row_text": {
                    "type": "string",
                    "description": "Text that identifies the target row (for example 'Confirmo y no asistio' or 'No asiste y no confirma')."
                }
            },
            "required": ["row_text"]
        }
    },
    {
        "name": "browser_extract_visible_table",
        "description": "Extract the currently visible table into structured headers and rows. Prefer heading_text when the page has multiple tables; Hermes will target the first visible table that follows that heading. Use this when browser_snapshot truncates long table content and you need every row.",
        "parameters": {
            "type": "object",
            "properties": {
                "heading_text": {
                    "type": "string",
                    "description": "Optional heading text that identifies the table section to extract, for example 'Detalle estado Confirmo y no asistió'."
                }
            },
            "required": []
        }
    },
    {
        "name": "browser_type",
        "description": "Type text into an input field identified by its ref ID. Clears the field first, then types the new text. Provide exactly one of text or secret_env_var. When secret_env_var is used, Hermes resolves the environment variable at runtime without exposing the secret value in tool progress or persisted traces. Requires browser_navigate and browser_snapshot to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The element reference from the snapshot (e.g., '@e3')"
                },
                "text": {
                    "type": "string",
                    "description": "The text to type into the field"
                },
                "secret_env_var": {
                    "type": "string",
                    "description": "Optional environment variable name to resolve and type at runtime for sensitive inputs. Provide this instead of text for passwords, tokens, and other secrets."
                }
            },
            "required": ["ref"]
        }
    },
    {
        "name": "browser_scroll",
        "description": "Scroll the page in a direction. Use this to reveal more content that may be below or above the current viewport. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down"],
                    "description": "Direction to scroll"
                }
            },
            "required": ["direction"]
        }
    },
    {
        "name": "browser_back",
        "description": "Navigate back to the previous page in browser history. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "browser_press",
        "description": "Press a keyboard key. Useful for submitting forms (Enter), navigating (Tab), or keyboard shortcuts. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key to press (e.g., 'Enter', 'Tab', 'Escape', 'ArrowDown')"
                }
            },
            "required": ["key"]
        }
    },
    {
        "name": "browser_close",
        "description": "Close the browser session and release resources. Call this when done with browser tasks to free up Browserbase session quota.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "browser_get_images",
        "description": "Get a list of all images on the current page with their URLs and alt text. Useful for finding images to analyze with the vision tool. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "browser_vision",
        "description": "Take a screenshot of the current page and analyze it with vision AI. Use this when you need to visually understand what's on the page - especially useful for CAPTCHAs, visual verification challenges, complex layouts, or when the text snapshot doesn't capture important visual information. Returns both the AI analysis and a screenshot_path that you can share with the user by including MEDIA:<screenshot_path> in your response. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "What you want to know about the page visually. Be specific about what you're looking for."
                },
                "annotate": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, overlay numbered [N] labels on interactive elements. Each [N] maps to ref @eN for subsequent browser commands. Useful for QA and spatial reasoning about page layout."
                }
            },
            "required": ["question"]
        }
    },
    {
        "name": "browser_console",
        "description": "Get browser console output and JavaScript errors from the current page. Returns console.log/warn/error/info messages and uncaught JS exceptions. Use this to detect silent JavaScript errors, failed API calls, and application warnings. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "clear": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, clear the message buffers after reading"
                }
            },
            "required": []
        }
    },
]


# ============================================================================
# Utility Functions
# ============================================================================

def _create_local_session(task_id: str) -> Dict[str, str]:
    import uuid
    session_name = f"h_{uuid.uuid4().hex[:10]}"
    logger.info("Created local browser session %s for task %s",
                session_name, task_id)
    return {
        "session_name": session_name,
        "bb_session_id": None,
        "cdp_url": None,
        "features": {"local": True},
    }


def _create_cdp_session(task_id: str, cdp_url: str) -> Dict[str, str]:
    """Create a session that connects to a user-supplied CDP endpoint."""
    import uuid
    session_name = f"cdp_{uuid.uuid4().hex[:10]}"
    logger.info("Created CDP browser session %s → %s for task %s",
                session_name, cdp_url, task_id)
    return {
        "session_name": session_name,
        "bb_session_id": None,
        "cdp_url": cdp_url,
        "features": {"cdp_override": True},
    }


def _get_session_info(task_id: Optional[str] = None) -> Dict[str, str]:
    """
    Get or create session info for the given task.
    
    In cloud mode, creates a Browserbase session with proxies enabled.
    In local mode, generates a session name for agent-browser --session.
    Also starts the inactivity cleanup thread and updates activity tracking.
    Thread-safe: multiple subagents can call this concurrently.
    
    Args:
        task_id: Unique identifier for the task
        
    Returns:
        Dict with session_name (always), bb_session_id + cdp_url (cloud only)
    """
    if task_id is None:
        task_id = "default"
    
    # Start the cleanup thread if not running (handles inactivity timeouts)
    _start_browser_cleanup_thread()
    
    # Update activity timestamp for this session
    _update_session_activity(task_id)
    
    with _cleanup_lock:
        # Check if we already have a session for this task
        if task_id in _active_sessions:
            return _active_sessions[task_id]
    
    # Create session outside the lock (network call in cloud mode)
    cdp_override = _get_cdp_override()
    if cdp_override:
        session_info = _create_cdp_session(task_id, cdp_override)
    else:
        provider = _get_cloud_provider()
        if provider is None:
            session_info = _create_local_session(task_id)
        else:
            session_info = provider.create_session(task_id)
    
    with _cleanup_lock:
        # Double-check: another thread may have created a session while we
        # were doing the network call. Use the existing one to avoid leaking
        # orphan cloud sessions.
        if task_id in _active_sessions:
            return _active_sessions[task_id]
        _active_sessions[task_id] = session_info
    
    return session_info


def _get_session_name(task_id: Optional[str] = None) -> str:
    """
    Get the session name for agent-browser CLI.
    
    Args:
        task_id: Unique identifier for the task
        
    Returns:
        Session name for agent-browser
    """
    session_info = _get_session_info(task_id)
    return session_info["session_name"]


def _find_agent_browser() -> str:
    """
    Find the agent-browser CLI executable.
    
    Checks in order: PATH, local node_modules/.bin/, npx fallback.
    
    Returns:
        Path to agent-browser executable
        
    Raises:
        FileNotFoundError: If agent-browser is not installed
    """

    # Check if it's in PATH (global install)
    which_result = shutil.which("agent-browser")
    if which_result:
        return which_result
    
    # Check local node_modules/.bin/ (npm install in repo root)
    repo_root = Path(__file__).parent.parent
    local_bin = repo_root / "node_modules" / ".bin" / "agent-browser"
    if local_bin.exists():
        return str(local_bin)
    
    # Check common npx locations
    npx_path = shutil.which("npx")
    if npx_path:
        return "npx agent-browser"
    
    raise FileNotFoundError(
        "agent-browser CLI not found. Install it with: npm install -g agent-browser\n"
        "Or run 'npm install' in the repo root to install locally.\n"
        "Or ensure npx is available in your PATH."
    )


def _extract_screenshot_path_from_text(text: str) -> Optional[str]:
    """Extract a screenshot file path from agent-browser human-readable output."""
    if not text:
        return None

    patterns = [
        r"Screenshot saved to ['\"](?P<path>/[^'\"]+?\.png)['\"]",
        r"Screenshot saved to (?P<path>/\S+?\.png)(?:\s|$)",
        r"(?P<path>/\S+?\.png)(?:\s|$)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            path = match.group("path").strip().strip("'\"")
            if path:
                return path

    return None


def _run_browser_command(
    task_id: str,
    command: str,
    args: List[str] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT
) -> Dict[str, Any]:
    """
    Run an agent-browser CLI command using our pre-created Browserbase session.
    
    Args:
        task_id: Task identifier to get the right session
        command: The command to run (e.g., "open", "click")
        args: Additional arguments for the command
        timeout: Command timeout in seconds
        
    Returns:
        Parsed JSON response from agent-browser
    """
    args = args or []
    
    # Build the command
    try:
        browser_cmd = _find_agent_browser()
    except FileNotFoundError as e:
        logger.warning("agent-browser CLI not found: %s", e)
        return {"success": False, "error": str(e)}
    
    from tools.interrupt import is_interrupted
    if is_interrupted():
        return {"success": False, "error": "Interrupted"}

    # Get session info (creates Browserbase session with proxies if needed)
    try:
        session_info = _get_session_info(task_id)
    except Exception as e:
        logger.warning("Failed to create browser session for task=%s: %s", task_id, e)
        return {"success": False, "error": f"Failed to create browser session: {str(e)}"}
    
    # Build the command with the appropriate backend flag.
    # Cloud mode: --cdp <websocket_url> connects to Browserbase.
    # Local mode: --session <name> launches a local headless Chromium.
    # The rest of the command (--json, command, args) is identical.
    if session_info.get("cdp_url"):
        # Cloud mode — connect to remote Browserbase browser via CDP
        # IMPORTANT: Do NOT use --session with --cdp. In agent-browser >=0.13,
        # --session creates a local browser instance and silently ignores --cdp.
        backend_args = ["--cdp", session_info["cdp_url"]]
    else:
        # Local mode — launch a headless Chromium instance
        backend_args = ["--session", session_info["session_name"]]

    cmd_parts = browser_cmd.split() + backend_args + [
        "--json",
        command
    ] + args
    
    try:
        # Give each task its own socket directory to prevent concurrency conflicts.
        # Without this, parallel workers fight over the same default socket path,
        # causing "Failed to create socket directory: Permission denied" errors.
        task_socket_dir = os.path.join(
            _socket_safe_tmpdir(),
            f"agent-browser-{session_info['session_name']}"
        )
        os.makedirs(task_socket_dir, mode=0o700, exist_ok=True)
        logger.debug("browser cmd=%s task=%s socket_dir=%s (%d chars)",
                     command, task_id, task_socket_dir, len(task_socket_dir))
        
        browser_env = {**os.environ}

        # Ensure PATH includes Hermes-managed Node first, then standard system dirs.
        hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        hermes_node_bin = str(hermes_home / "node" / "bin")

        existing_path = browser_env.get("PATH", "")
        path_parts = [p for p in existing_path.split(":") if p]
        candidate_dirs = [hermes_node_bin] + [p for p in _SANE_PATH.split(":") if p]

        for part in reversed(candidate_dirs):
            if os.path.isdir(part) and part not in path_parts:
                path_parts.insert(0, part)

        browser_env["PATH"] = ":".join(path_parts)
        browser_env["AGENT_BROWSER_SOCKET_DIR"] = task_socket_dir
        
        # Use temp files for stdout/stderr instead of pipes.
        # agent-browser starts a background daemon that inherits file
        # descriptors.  With capture_output=True (pipes), the daemon keeps
        # the pipe fds open after the CLI exits, so communicate() never
        # sees EOF and blocks until the timeout fires.
        stdout_path = os.path.join(task_socket_dir, f"_stdout_{command}")
        stderr_path = os.path.join(task_socket_dir, f"_stderr_{command}")
        stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            proc = subprocess.Popen(
                cmd_parts,
                stdout=stdout_fd,
                stderr=stderr_fd,
                stdin=subprocess.DEVNULL,
                env=browser_env,
            )
        finally:
            os.close(stdout_fd)
            os.close(stderr_fd)

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            logger.warning("browser '%s' timed out after %ds (task=%s, socket_dir=%s)",
                           command, timeout, task_id, task_socket_dir)
            return {"success": False, "error": f"Command timed out after {timeout} seconds"}

        with open(stdout_path, "r") as f:
            stdout = f.read()
        with open(stderr_path, "r") as f:
            stderr = f.read()
        returncode = proc.returncode

        # Clean up temp files (best-effort)
        for p in (stdout_path, stderr_path):
            try:
                os.unlink(p)
            except OSError:
                pass

        # Log stderr for diagnostics — use warning level on failure so it's visible
        if stderr and stderr.strip():
            level = logging.WARNING if returncode != 0 else logging.DEBUG
            logger.log(level, "browser '%s' stderr: %s", command, stderr.strip()[:500])
        
        # Log empty output as warning — common sign of broken agent-browser
        if not stdout.strip() and returncode == 0:
            logger.warning("browser '%s' returned empty stdout with rc=0. "
                           "cmd=%s stderr=%s",
                           command, " ".join(cmd_parts[:4]) + "...",
                           (stderr or "")[:200])

        stdout_text = stdout.strip()

        if stdout_text:
            try:
                parsed = json.loads(stdout_text)
                # Warn if snapshot came back empty (common sign of daemon/CDP issues)
                if command == "snapshot" and parsed.get("success"):
                    snap_data = parsed.get("data", {})
                    if not snap_data.get("snapshot") and not snap_data.get("refs"):
                        logger.warning("snapshot returned empty content. "
                                       "Possible stale daemon or CDP connection issue. "
                                       "returncode=%s", returncode)
                return parsed
            except json.JSONDecodeError:
                raw = stdout_text[:2000]
                logger.warning("browser '%s' returned non-JSON output (rc=%s): %s",
                               command, returncode, raw[:500])

                if command == "screenshot":
                    stderr_text = (stderr or "").strip()
                    combined_text = "\n".join(
                        part for part in [stdout_text, stderr_text] if part
                    )
                    recovered_path = _extract_screenshot_path_from_text(combined_text)

                    if recovered_path and Path(recovered_path).exists():
                        logger.info(
                            "browser 'screenshot' recovered file from non-JSON output: %s",
                            recovered_path,
                        )
                        return {
                            "success": True,
                            "data": {
                                "path": recovered_path,
                                "raw": raw,
                            },
                        }

                return {
                    "success": False,
                    "error": f"Non-JSON output from agent-browser for '{command}': {raw}"
                }
        
        # Check for errors
        if returncode != 0:
            error_msg = stderr.strip() if stderr else f"Command failed with code {returncode}"
            logger.warning("browser '%s' failed (rc=%s): %s", command, returncode, error_msg[:300])
            return {"success": False, "error": error_msg}
        
        return {"success": True, "data": {}}
        
    except Exception as e:
        logger.warning("browser '%s' exception: %s", command, e, exc_info=True)
        return {"success": False, "error": str(e)}


def _extract_relevant_content(
    snapshot_text: str,
    user_task: Optional[str] = None
) -> str:
    """Use LLM to extract relevant content from a snapshot based on the user's task.

    Falls back to simple truncation when no auxiliary text model is configured.
    """
    if user_task:
        extraction_prompt = (
            f"You are a content extractor for a browser automation agent.\n\n"
            f"The user's task is: {user_task}\n\n"
            f"Given the following page snapshot (accessibility tree representation), "
            f"extract and summarize the most relevant information for completing this task. Focus on:\n"
            f"1. Interactive elements (buttons, links, inputs) that might be needed\n"
            f"2. Text content relevant to the task (prices, descriptions, headings, important info)\n"
            f"3. Navigation structure if relevant\n\n"
            f"Keep ref IDs (like [ref=e5]) for interactive elements so the agent can use them.\n\n"
            f"Page Snapshot:\n{snapshot_text}\n\n"
            f"Provide a concise summary that preserves actionable information and relevant content."
        )
    else:
        extraction_prompt = (
            f"Summarize this page snapshot, preserving:\n"
            f"1. All interactive elements with their ref IDs (like [ref=e5])\n"
            f"2. Key text content and headings\n"
            f"3. Important information visible on the page\n\n"
            f"Page Snapshot:\n{snapshot_text}\n\n"
            f"Provide a concise summary focused on interactive elements and key content."
        )

    try:
        call_kwargs = {
            "task": "web_extract",
            "messages": [{"role": "user", "content": extraction_prompt}],
            "max_tokens": 4000,
            "temperature": 0.1,
        }
        model = _get_extraction_model()
        if model:
            call_kwargs["model"] = model
        response = call_llm(**call_kwargs)
        return response.choices[0].message.content
    except Exception:
        return _truncate_snapshot(snapshot_text)


def _truncate_snapshot(snapshot_text: str, max_chars: int = 8000) -> str:
    """
    Simple truncation fallback for snapshots.
    
    Args:
        snapshot_text: The snapshot text to truncate
        max_chars: Maximum characters to keep
        
    Returns:
        Truncated text with indicator if truncated
    """
    if len(snapshot_text) <= max_chars:
        return snapshot_text
    
    return snapshot_text[:max_chars] + "\n\n[... content truncated ...]"


def _normalize_browser_ref(ref: str) -> str:
    """Normalize snapshot refs so browser commands always receive @eN."""
    return ref if ref.startswith("@") else f"@{ref}"


def _get_browser_attribute(task_id: str, ref: str, attribute: str) -> Optional[str]:
    """Read an element attribute via agent-browser and return a stripped value."""
    result = _run_browser_command(task_id, "getattribute", [ref, attribute])
    if not result.get("success"):
        return None

    data = result.get("data", {})
    value = data.get("value")
    if value is None:
        return None

    value_str = str(value).strip()
    return value_str or None


def _get_compact_snapshot(task_id: str) -> Dict[str, Any]:
    """Fetch the current compact accessibility snapshot for a task."""
    return _run_browser_command(task_id, "snapshot", ["-c"])


def _snapshot_data(snapshot_result: Dict[str, Any]) -> Dict[str, Any]:
    """Return the snapshot payload if the command succeeded."""
    if not snapshot_result.get("success"):
        return {}

    data = snapshot_result.get("data", {})
    return data if isinstance(data, dict) else {}


def _snapshot_text(snapshot_result: Dict[str, Any]) -> str:
    """Return the raw snapshot text from a snapshot command result."""
    return str(_snapshot_data(snapshot_result).get("snapshot") or "")


def _snapshot_refs(snapshot_result: Dict[str, Any]) -> Dict[str, Any]:
    """Return the refs map from a snapshot command result."""
    refs = _snapshot_data(snapshot_result).get("refs", {})
    return refs if isinstance(refs, dict) else {}


def _extract_snapshot_ref_text(entry: Any) -> Optional[str]:
    """Extract a usable label/value from a snapshot ref entry."""
    if isinstance(entry, str):
        value = entry.strip()
        return value or None

    if isinstance(entry, dict):
        for key in ("value", "label", "text", "name", "title", "aria_label", "ariaLabel"):
            value = entry.get(key)
            if value is not None:
                value_str = str(value).strip()
                if value_str:
                    return value_str

    return None


def _find_snapshot_ref_line(snapshot_text: str, ref: str) -> Optional[str]:
    """Return the line in a snapshot that contains the requested ref."""
    if not snapshot_text:
        return None

    normalized_ref = ref[1:] if ref.startswith("@") else ref
    ref_pattern = re.compile(
        rf"^(?P<indent>\s*).*\[ref={re.escape(normalized_ref)}\](?::)?(?:\s.*)?$"
    )
    for line in snapshot_text.splitlines():
        if ref_pattern.match(line):
            return line
    return None


def _extract_snapshot_line_text(line: str) -> Optional[str]:
    """Extract the most likely human-readable label from a snapshot line."""
    if not line:
        return None

    quoted_values = re.findall(r'"([^"]+)"', line)
    if quoted_values:
        value = quoted_values[-1].strip()
        return value or None

    before_ref = line.split("[ref=", 1)[0]
    before_ref = re.sub(r"^\s*-\s*", "", before_ref).strip()
    before_ref = before_ref.rstrip(":").strip()
    return before_ref or None


def _snapshot_ref_index(snapshot_text: str, ref: str, token: str) -> Optional[int]:
    """Return the zero-based index of a ref among lines containing a token."""
    if not snapshot_text:
        return None

    normalized_ref = ref if ref.startswith("@") else f"@{ref}"
    target = normalized_ref[1:]
    index = 0

    for line in snapshot_text.splitlines():
        if token not in line or "[ref=" not in line:
            continue

        match = re.search(r"\[ref=(e\d+)\]", line)
        if not match:
            continue

        current_ref = "@" + match.group(1)
        if current_ref == normalized_ref or match.group(1) == target:
            return index
        index += 1

    return None


def _resolve_snapshot_ref_text(task_id: str, ref: str, snapshot_result: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Resolve a ref to its human-readable text using the current snapshot."""
    effective_snapshot = snapshot_result or _get_compact_snapshot(task_id)
    if not effective_snapshot.get("success"):
        return None

    refs = _snapshot_refs(effective_snapshot)
    normalized_ref = ref if ref.startswith("@") else f"@{ref}"
    short_ref = normalized_ref[1:]

    entry = None
    for key in (normalized_ref, short_ref, ref):
        if key in refs:
            entry = refs[key]
            break

    text = _extract_snapshot_ref_text(entry)
    if text:
        return text

    snapshot_text = _snapshot_text(effective_snapshot)
    line = _find_snapshot_ref_line(snapshot_text, normalized_ref)
    if not line:
        return None

    return _extract_snapshot_line_text(line)


def _resolve_select_option_value(
    task_id: str,
    option_ref: str,
    snapshot_result: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Resolve an option ref to its form value, falling back to visible text."""
    normalized_option_ref = _normalize_browser_ref(option_ref)
    option_value = _get_browser_attribute(task_id, normalized_option_ref, "value")
    if option_value is not None:
        return option_value

    return _resolve_snapshot_ref_text(
        task_id,
        normalized_option_ref,
        snapshot_result=snapshot_result,
    )


def _resolve_select_value_from_label(
    task_id: str,
    select_ref: str,
    selected_value: str,
    snapshot_result: Optional[Dict[str, Any]] = None,
) -> str:
    """Translate visible option text into its real value when possible."""
    effective_snapshot = snapshot_result or _get_compact_snapshot(task_id)
    if not effective_snapshot.get("success"):
        return selected_value

    snapshot_text = _snapshot_text(effective_snapshot)
    if not snapshot_text:
        return selected_value

    normalized_select_ref = select_ref[1:] if select_ref.startswith("@") else select_ref
    lines = snapshot_text.splitlines()
    target_label = selected_value.strip()
    ref_pattern = re.compile(
        rf"^(?P<indent>\s*).*\[ref={re.escape(normalized_select_ref)}\](?::)?(?:\s.*)?$"
    )
    option_ref_pattern = re.compile(r"\[ref=(e\d+)\]")

    for idx, line in enumerate(lines):
        match = ref_pattern.match(line)
        if not match:
            continue

        base_indent = len(match.group("indent"))
        for child_line in lines[idx + 1:]:
            if not child_line.strip():
                continue

            child_indent = len(child_line) - len(child_line.lstrip(" "))
            if child_indent <= base_indent:
                break

            if "option" not in child_line:
                continue

            option_ref_match = option_ref_pattern.search(child_line)
            if not option_ref_match:
                continue

            option_label = _extract_snapshot_line_text(child_line)
            if option_label != target_label:
                continue

            option_ref = "@" + option_ref_match.group(1)
            resolved_value = _get_browser_attribute(task_id, option_ref, "value")
            return resolved_value or selected_value

        break

    return selected_value


def _resolve_combobox_selector(task_id: str, ref: str, snapshot_result: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Resolve a combobox ref to a stable Playwright locator string."""
    effective_snapshot = snapshot_result or _get_compact_snapshot(task_id)
    if not effective_snapshot.get("success"):
        return None

    entry = _snapshot_refs(effective_snapshot).get(ref[1:] if ref.startswith("@") else ref)
    if not isinstance(entry, dict) or entry.get("role") != "combobox":
        return None

    snapshot_text = _snapshot_text(effective_snapshot)
    combobox_index = _snapshot_ref_index(snapshot_text, ref, "combobox")
    if combobox_index is None:
        return None

    return f"select >> nth={combobox_index}"


def _get_snapshot_href(task_id: str, ref: str, snapshot_result: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Resolve a link href from the current compact snapshot tree."""
    result = snapshot_result or _get_compact_snapshot(task_id)
    if not result.get("success"):
        return None

    snapshot_text = _snapshot_text(result)
    if not snapshot_text:
        return None

    normalized_ref = ref[1:] if ref.startswith("@") else ref
    ref_pattern = re.compile(
        rf"^(?P<indent>\s*).*\[ref={re.escape(normalized_ref)}\](?::)?(?:\s.*)?$"
    )
    url_pattern = re.compile(r'^\s*-\s+/url:\s+"?(?P<url>[^"\n]+)"?\s*$')
    lines = snapshot_text.splitlines()

    for idx, line in enumerate(lines):
        match = ref_pattern.match(line)
        if not match:
            continue

        base_indent = len(match.group("indent"))
        for child_line in lines[idx + 1:]:
            if not child_line.strip():
                continue

            child_indent = len(child_line) - len(child_line.lstrip(" "))
            if child_indent <= base_indent:
                break

            url_match = url_pattern.match(child_line)
            if url_match:
                href = url_match.group("url").strip()
                return href or None

        break

    return None


def _is_invalid_ref_or_selector_error(error: Optional[str]) -> bool:
    """Return True for ref/selector errors that should trigger a retry."""
    if not error:
        return False

    lowered = error.lower()
    invalid_markers = (
        "invalid ref",
        "invalid selector",
        "ref invalid",
        "selector invalid",
        "ref not found",
        "selector not found",
        "ref missing",
        "selector missing",
        "no ref map",
        "stale element",
        "element not found",
        "not found or not visible",
        "not visible",
        "node not found",
        "could not find",
        "unable to find",
        "not attached",
        "unsupported token",
        "parsing css selector",
        "css.escape",
    )
    return any(marker in lowered for marker in invalid_markers)


def _is_ambiguous_locator_error(error: Optional[str]) -> bool:
    """Return True when a locator resolves to multiple elements."""
    if not error:
        return False

    lowered = error.lower()
    ambiguous_markers = (
        "matched 2 elements",
        "matched 3 elements",
        "matched 4 elements",
        "strict mode violation",
        "resolved to",
    )
    return any(marker in lowered for marker in ambiguous_markers)


def _is_command_timeout_error(error: Optional[str]) -> bool:
    """Return True when the browser command timed out."""
    if not error:
        return False

    lowered = error.lower()
    return "timed out" in lowered or "timeout" in lowered


def _is_navigable_href(href: Optional[str]) -> bool:
    """Return True when an href should be treated as full-page navigation."""
    if not href:
        return False

    lowered = href.strip().lower()
    if not lowered:
        return False

    blocked_prefixes = ("#", "javascript:", "mailto:", "tel:")
    return not lowered.startswith(blocked_prefixes)


def _resolve_click_navigation_url(
    task_id: str,
    ref: str,
    snapshot_result: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Resolve a clicked link ref into an absolute URL when safe to navigate."""
    href = _get_snapshot_href(task_id, ref, snapshot_result=snapshot_result)
    if href is None:
        href = _get_browser_attribute(task_id, ref, "href")
    if not _is_navigable_href(href):
        return None

    parsed = urlparse(href)
    if parsed.scheme:
        return href

    session_info = _get_session_info(task_id)
    current_url = str(session_info.get("last_url") or "").strip()

    if not current_url:
        current_url_result = _run_browser_command(task_id, "url", [])
        if not current_url_result.get("success"):
            return None
        current_url = str(current_url_result.get("data", {}).get("url") or "").strip()
        if current_url:
            session_info["last_url"] = current_url

    if not current_url:
        return None

    return urljoin(current_url, href)


def _resolve_select_dom_index(ref: str, snapshot_result: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """Resolve a combobox ref/selector to a DOM select index for JS fallback."""
    if ref.startswith("@"):
        effective_snapshot = snapshot_result or {}
        snapshot_text = _snapshot_text(effective_snapshot)
        if not snapshot_text:
            return None
        return _snapshot_ref_index(snapshot_text, ref, "combobox")

    nth_match = re.search(r"nth\s*=\s*(\d+)", ref)
    if nth_match:
        return int(nth_match.group(1))

    return None


def _resolve_editable_dom_index(ref: str, snapshot_result: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """Resolve an editable ref to a DOM index among visible text-entry controls."""
    if not ref.startswith("@"):
        return None

    effective_snapshot = snapshot_result or {}
    snapshot_text = _snapshot_text(effective_snapshot)
    if not snapshot_text:
        return None

    refs = _snapshot_refs(effective_snapshot)
    entry = refs.get(ref[1:]) or refs.get(ref)
    tokens: List[str] = []

    if isinstance(entry, dict):
        role = str(entry.get("role") or "").strip().lower()
        if role:
            tokens.append(role)

    line = _find_snapshot_ref_line(snapshot_text, ref)
    if line:
        lowered_line = line.lower()
        for marker in EDITABLE_SNAPSHOT_ROLE_MARKERS:
            if marker in lowered_line and marker not in tokens:
                tokens.append(marker)

    for marker in EDITABLE_SNAPSHOT_ROLE_MARKERS:
        if marker not in tokens:
            tokens.append(marker)

    for token in tokens:
        index = _snapshot_ref_index(snapshot_text, ref, token)
        if index is not None:
            return index

    return None


def _build_select_eval_script(ref: str, selected_value: str, snapshot_result: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Build a DOM-level select fallback script for stubborn dropdowns."""
    dom_index = _resolve_select_dom_index(ref, snapshot_result=snapshot_result)
    if dom_index is None:
        return None

    target_value = json.dumps(selected_value)

    return f"""(() => {{
  const selects = Array.from(document.querySelectorAll("select"));
  const preferredIndex = {dom_index};
  const targetValue = {target_value};

  const resolveMatch = (selectEl, index) => {{
    if (!selectEl) {{
      return null;
    }}

    const options = Array.from(selectEl.options || []);
    const matchedOption =
      options.find(option => String(option.value) === targetValue) ||
      options.find(option => (option.textContent || "").trim() === targetValue);

    return {{ selectEl, options, matchedOption, index }};
  }};

  let resolved = resolveMatch(selects[preferredIndex] || null, preferredIndex);
  if (!resolved || !resolved.matchedOption) {{
    for (let index = 0; index < selects.length; index += 1) {{
      if (index === preferredIndex) {{
        continue;
      }}
      const candidate = resolveMatch(selects[index], index);
      if (candidate && candidate.matchedOption) {{
        resolved = candidate;
        break;
      }}
      if (!resolved && candidate) {{
        resolved = candidate;
      }}
    }}
  }}

  const selectEl = resolved?.selectEl || null;
  if (!selectEl) {{
    return JSON.stringify({{ success: false, error: "Select element not found for DOM fallback." }});
  }}

  const matchedOption = resolved?.matchedOption || null;
  if (!matchedOption) {{
    return JSON.stringify({{
      success: false,
      error: `Option ${{targetValue}} not found in DOM fallback.`
    }});
  }}

  selectEl.value = matchedOption.value;
  matchedOption.selected = true;
  selectEl.dispatchEvent(new Event("input", {{ bubbles: true }}));
  selectEl.dispatchEvent(new Event("change", {{ bubbles: true }}));

  if (window.jQuery) {{
    try {{
      window.jQuery(selectEl).trigger("input");
      window.jQuery(selectEl).trigger("change");
    }} catch (_) {{
      // Ignore jQuery trigger issues and rely on native events.
    }}
  }}

  return JSON.stringify({{
    success: true,
    selected: matchedOption.value || targetValue,
    matched_text: (matchedOption.textContent || "").trim(),
    resolved_index: resolved?.index ?? preferredIndex
  }});
}})()"""


def _select_via_eval(
    task_id: str,
    ref: str,
    selected_value: str,
    snapshot_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Fallback selection by setting the DOM value and dispatching events."""
    script = _build_select_eval_script(ref, selected_value, snapshot_result=snapshot_result)
    if not script:
        return {
            "success": False,
            "error": "Could not resolve a stable DOM select index for fallback.",
        }

    result = _run_browser_command(task_id, "eval", [script])
    if not result.get("success"):
        return result

    raw_result = result.get("data", {}).get("result")
    parsed_result: Optional[Dict[str, Any]] = None
    if isinstance(raw_result, str):
        try:
            maybe_parsed = json.loads(raw_result)
            if isinstance(maybe_parsed, dict):
                parsed_result = maybe_parsed
        except json.JSONDecodeError:
            parsed_result = None
    elif isinstance(raw_result, dict):
        parsed_result = raw_result

    if parsed_result and parsed_result.get("success"):
        return {"success": True, "data": parsed_result}

    return {
        "success": False,
        "error": (
            (parsed_result or {}).get("error")
            or result.get("error")
            or "DOM select fallback failed."
        ),
    }


def _build_fill_eval_script(
    preferred_index: Optional[int],
    typed_text: str,
    preferred_label: Optional[str] = None,
) -> str:
    """Build a DOM-level fill fallback script for dynamic editable controls."""
    target_value = json.dumps(typed_text)
    target_label = json.dumps(preferred_label or "")
    preferred_index_literal = "null" if preferred_index is None else str(preferred_index)

    return f"""(() => {{
  const normalize = (value) => String(value || "")
    .normalize("NFD")
    .replace(/[\\u0300-\\u036f]/g, "")
    .replace(/\\s+/g, " ")
    .trim()
    .toLowerCase();

  const isVisible = (el) => Boolean(
    el &&
    typeof el.getClientRects === "function" &&
    el.getClientRects().length
  );

  const getLabelText = (el) => {{
    if (!el) {{
      return "";
    }}
    const directAttrs = [
      el.getAttribute && el.getAttribute("aria-label"),
      el.getAttribute && el.getAttribute("placeholder"),
      el.getAttribute && el.getAttribute("title"),
      el.name,
      el.id,
    ];
    for (const value of directAttrs) {{
      if (String(value || "").trim()) {{
        return String(value).trim();
      }}
    }}

    if (el.id) {{
      const label = document.querySelector(`label[for="${{CSS.escape(el.id)}}"]`);
      if (label && String(label.textContent || "").trim()) {{
        return String(label.textContent).trim();
      }}
    }}

    const wrappingLabel = el.closest("label");
    if (wrappingLabel && String(wrappingLabel.textContent || "").trim()) {{
      return String(wrappingLabel.textContent).trim();
    }}

    return "";
  }};

  const setNativeValue = (el, nextValue) => {{
    if (el && "value" in el) {{
      const descriptor =
        Object.getOwnPropertyDescriptor(el.constructor?.prototype || {{}}, "value") ||
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value") ||
        Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");
      if (descriptor && typeof descriptor.set === "function") {{
        descriptor.set.call(el, nextValue);
        return;
      }}
      el.value = nextValue;
      return;
    }}

    if (el && el.isContentEditable) {{
      el.textContent = nextValue;
    }}
  }};

  const targetValue = {target_value};
  const normalizedTargetLabel = normalize({target_label});
  const preferredIndex = {preferred_index_literal};

  const candidates = Array.from(document.querySelectorAll(
    'input:not([type="hidden"]):not([disabled]), textarea:not([disabled]), [contenteditable="true"], [contenteditable=""], [contenteditable="plaintext-only"]'
  )).filter(isVisible);

  if (!candidates.length) {{
    return JSON.stringify({{ success: false, error: "No visible editable elements found for DOM fill fallback." }});
  }}

  const labeledCandidate = normalizedTargetLabel
    ? candidates.find((candidate) => normalize(getLabelText(candidate)).includes(normalizedTargetLabel))
    : null;

  let resolved = null;
  if (Number.isInteger(preferredIndex) && preferredIndex >= 0 && preferredIndex < candidates.length) {{
    resolved = candidates[preferredIndex];
  }}
  if ((!resolved || !isVisible(resolved)) && labeledCandidate) {{
    resolved = labeledCandidate;
  }}
  if (!resolved && candidates.length === 1) {{
    resolved = candidates[0];
  }}
  if (!resolved) {{
    return JSON.stringify({{
      success: false,
      error: "Editable element not found for DOM fill fallback."
    }});
  }}

  try {{
    if (typeof resolved.focus === "function") {{
      resolved.focus();
    }}
  }} catch (_) {{
    // Ignore focus issues and still attempt to set the value.
  }}

  setNativeValue(resolved, targetValue);
  resolved.dispatchEvent(new Event("input", {{ bubbles: true }}));
  resolved.dispatchEvent(new Event("change", {{ bubbles: true }}));

  if (window.jQuery) {{
    try {{
      window.jQuery(resolved).trigger("input");
      window.jQuery(resolved).trigger("change");
    }} catch (_) {{
      // Ignore jQuery trigger issues and rely on native events.
    }}
  }}

  return JSON.stringify({{
    success: true,
    resolved_index: candidates.indexOf(resolved),
    matched_label: getLabelText(resolved) || null
  }});
}})()"""


def _fill_via_eval(
    task_id: str,
    ref: str,
    typed_text: str,
    snapshot_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Fallback fill by targeting a visible editable control in the DOM."""
    effective_snapshot = snapshot_result or _get_compact_snapshot(task_id)
    dom_index = _resolve_editable_dom_index(ref, snapshot_result=effective_snapshot)
    preferred_label = _resolve_snapshot_ref_text(
        task_id,
        ref,
        snapshot_result=effective_snapshot,
    )
    script = _build_fill_eval_script(dom_index, typed_text, preferred_label=preferred_label)

    result = _run_browser_command(task_id, "eval", [script])
    if not result.get("success"):
        return result

    parsed_result = _parse_eval_result_dict(result.get("data", {}).get("result"))
    if parsed_result and parsed_result.get("success"):
        return {"success": True, "data": parsed_result}

    return {
        "success": False,
        "error": (
            (parsed_result or {}).get("error")
            or result.get("error")
            or "DOM fill fallback failed."
        ),
    }


def _run_snapshot_capture(task_id: str, full: bool) -> Dict[str, Any]:
    """Capture a single raw snapshot result."""
    args: List[str] = []
    if not full:
        args.append("-c")
    return _run_browser_command(task_id, "snapshot", args)


def _score_snapshot_result(result: Dict[str, Any]) -> tuple[int, int]:
    """Rank snapshot results by element count first, then raw snapshot length."""
    if not result.get("success"):
        return (-1, -1)

    refs = _snapshot_refs(result)
    snapshot_text = _snapshot_text(result)
    return (len(refs), len(snapshot_text))


def _build_snapshot_response(
    result: Dict[str, Any],
    user_task: Optional[str] = None,
    *,
    stabilize: bool = False,
    attempt_count: int = 1,
    selected_attempt: Optional[int] = 1,
) -> str:
    """Convert a raw snapshot command result into the public tool response."""
    if result.get("success"):
        data = result.get("data", {})
        snapshot_text = data.get("snapshot", "")
        refs = data.get("refs", {})

        if len(snapshot_text) > SNAPSHOT_SUMMARIZE_THRESHOLD and user_task:
            snapshot_text = _extract_relevant_content(snapshot_text, user_task)
        elif len(snapshot_text) > SNAPSHOT_SUMMARIZE_THRESHOLD:
            snapshot_text = _truncate_snapshot(snapshot_text)

        response: Dict[str, Any] = {
            "success": True,
            "snapshot": snapshot_text,
            "element_count": len(refs) if refs else 0,
        }
        if stabilize:
            response.update(
                {
                    "stabilized": True,
                    "attempt_count": attempt_count,
                    "selected_attempt": selected_attempt,
                }
            )
        return json.dumps(response, ensure_ascii=False)

    response = {
        "success": False,
        "error": result.get("error", "Failed to get snapshot"),
    }
    if stabilize:
        response.update(
            {
                "stabilized": True,
                "attempt_count": attempt_count,
                "selected_attempt": selected_attempt,
            }
        )
    return json.dumps(response, ensure_ascii=False)


def _parse_eval_result_dict(raw_result: Any) -> Optional[Dict[str, Any]]:
    """Normalize an eval command result into a dictionary when possible."""
    if isinstance(raw_result, dict):
        return raw_result

    if isinstance(raw_result, str):
        try:
            parsed = json.loads(raw_result)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed

    return None


def _build_select_option_probe_script(
    ref: str,
    selected_value: str,
    snapshot_result: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Build a DOM probe that checks whether a target option is selectable yet."""
    dom_index = _resolve_select_dom_index(ref, snapshot_result=snapshot_result)
    if dom_index is None:
        return None

    target_value = json.dumps(selected_value)

    return f"""(() => {{
  const selects = Array.from(document.querySelectorAll("select"));
  const preferredIndex = {dom_index};
  const targetValue = {target_value};

  const resolveMatch = (selectEl, index) => {{
    if (!selectEl) {{
      return null;
    }}

    const options = Array.from(selectEl.options || []);
    const matchedOption =
      options.find(option => String(option.value) === targetValue) ||
      options.find(option => (option.textContent || "").trim() === targetValue);

    return {{ selectEl, options, matchedOption, index }};
  }};

  let resolved = resolveMatch(selects[preferredIndex] || null, preferredIndex);
  if (!resolved || !resolved.matchedOption) {{
    for (let index = 0; index < selects.length; index += 1) {{
      if (index === preferredIndex) {{
        continue;
      }}
      const candidate = resolveMatch(selects[index], index);
      if (candidate && candidate.matchedOption) {{
        resolved = candidate;
        break;
      }}
      if (!resolved && candidate) {{
        resolved = candidate;
      }}
    }}
  }}

  const selectEl = resolved?.selectEl || null;
  if (!selectEl) {{
    return JSON.stringify({{
      success: false,
      ready: false,
      error: "Select element not found while waiting for option."
    }});
  }}

  const matchedOption = resolved?.matchedOption || null;
  const options = resolved?.options || Array.from(selectEl.options || []);

  return JSON.stringify({{
    success: true,
    ready: Boolean(matchedOption) && !selectEl.disabled,
    disabled: Boolean(selectEl.disabled),
    matched_value: matchedOption ? String(matchedOption.value || "") : null,
    matched_text: matchedOption ? (matchedOption.textContent || "").trim() : null,
    option_count: options.length,
    resolved_index: resolved?.index ?? preferredIndex
  }});
}})()"""


def _wait_for_select_option(
    task_id: str,
    ref: str,
    selected_value: str,
    snapshot_result: Optional[Dict[str, Any]] = None,
    timeout_seconds: float = SELECT_OPTION_WAIT_TIMEOUT,
) -> Dict[str, Any]:
    """Wait briefly for a dependent dropdown option to become selectable."""
    script = _build_select_option_probe_script(
        ref,
        selected_value,
        snapshot_result=snapshot_result,
    )
    if not script:
        return {"success": False, "ready": False, "error": "No stable select index available."}

    deadline = time.time() + max(0.0, timeout_seconds)
    last_result: Dict[str, Any] = {
        "success": False,
        "ready": False,
        "error": "Timed out waiting for select option.",
    }

    while True:
        probe = _run_browser_command(task_id, "eval", [script])
        if not probe.get("success"):
            last_result = {
                "success": False,
                "ready": False,
                "error": probe.get("error", "Select readiness probe failed."),
            }
        else:
            parsed = _parse_eval_result_dict(probe.get("data", {}).get("result")) or {}
            if parsed:
                last_result = parsed
                if parsed.get("ready"):
                    return parsed

        if time.time() >= deadline:
            return last_result

        time.sleep(SELECT_OPTION_POLL_INTERVAL)


def _build_row_detail_click_script(row_text: str) -> str:
    """Build a DOM click script for icon-only detail controls inside table rows."""
    row_text_json = json.dumps(row_text)
    return f"""(() => {{
  const normalize = (value) => String(value || "")
    .normalize("NFD")
    .replace(/[\\u0300-\\u036f]/g, "")
    .replace(/\\s+/g, " ")
    .trim()
    .toLowerCase();

  const target = normalize({row_text_json});
  if (!target) {{
    return JSON.stringify({{
      success: false,
      error: "row_text is required."
    }});
  }}

  const isVisible = (el) => Boolean(
    el &&
    typeof el.getClientRects === "function" &&
    el.getClientRects().length
  );

  const uniqueVisible = (elements) => {{
    const seen = new Set();
    const result = [];
    for (const element of elements) {{
      if (!element || seen.has(element) || !isVisible(element)) {{
        continue;
      }}
      seen.add(element);
      result.push(element);
    }}
    return result;
  }};

  const clickElement = (element) => {{
    if (!element) {{
      return false;
    }}
    try {{
      element.scrollIntoView({{ block: "center", inline: "center" }});
    }} catch (error) {{
      // Ignore scroll failures and still attempt the click.
    }}

    const event = new MouseEvent("click", {{
      bubbles: true,
      cancelable: true,
      view: window,
    }});

    if (typeof element.click === "function") {{
      element.click();
      return true;
    }}

    return element.dispatchEvent(event);
  }};

  const interactiveSelectors = [
    "a[href]",
    "button",
    "[role='button']",
    "input[type='button']",
    "input[type='submit']",
    "[onclick]"
  ].join(",");
  const iconSelectors = ["img", "svg", "i", "span"].join(",");
  const rows = Array.from(document.querySelectorAll("tr, [role='row']")).filter((row) =>
    normalize(row.innerText || row.textContent).includes(target)
  );

  if (!rows.length) {{
    return JSON.stringify({{
      success: false,
      error: `No table row found matching "${{target}}".`
    }});
  }}

  for (const row of rows) {{
    const cells = Array.from(row.querySelectorAll("td, th"));
    const lastCell = cells[cells.length - 1] || row;
    const candidates = uniqueVisible([
      ...Array.from(lastCell.querySelectorAll(interactiveSelectors)),
      ...Array.from(lastCell.querySelectorAll(iconSelectors)),
      ...Array.from(row.querySelectorAll(interactiveSelectors)),
    ]);

    const clicked = candidates[candidates.length - 1] || null;
    if (!clicked) {{
      continue;
    }}

    if (!clickElement(clicked)) {{
      continue;
    }}

    return JSON.stringify({{
      success: true,
      row_text: {row_text_json},
      matched_row_text: String(row.innerText || row.textContent || "").replace(/\\s+/g, " ").trim(),
      clicked_tag: String(clicked.tagName || "").toLowerCase(),
      clicked_text: String(clicked.innerText || clicked.textContent || "").replace(/\\s+/g, " ").trim() || null,
      clicked_href: typeof clicked.getAttribute === "function" ? clicked.getAttribute("href") : null,
    }});
  }}

  return JSON.stringify({{
    success: false,
    error: `No clickable detail control found in row matching "${{target}}".`
  }});
}})()"""


def _build_visible_table_extract_script(heading_text: Optional[str] = None) -> str:
    """Build a DOM extraction script for a visible table near a heading."""
    heading_text_json = json.dumps(heading_text or "")
    return f"""(() => {{
  const normalize = (value) => String(value || "")
    .normalize("NFD")
    .replace(/[\\u0300-\\u036f]/g, "")
    .replace(/\\s+/g, " ")
    .trim()
    .toLowerCase();

  const targetHeading = normalize({heading_text_json});

  const isVisible = (el) => Boolean(
    el &&
    typeof el.getClientRects === "function" &&
    el.getClientRects().length
  );

  const textOf = (value) => String(value || "").replace(/\\s+/g, " ").trim();

  const getRowCells = (row) => {{
    const roleCells = Array.from(row.querySelectorAll(":scope > [role='cell'], :scope > [role='columnheader']"));
    if (roleCells.length) {{
      return roleCells;
    }}
    return Array.from(row.querySelectorAll(":scope > td, :scope > th"));
  }};

  const getRows = (tableEl) => {{
    if (!tableEl) {{
      return [];
    }}
    const rows = Array.from(tableEl.querySelectorAll("tr, [role='row']"))
      .filter((row) => isVisible(row) && getRowCells(row).length);
    if (rows.length) {{
      return rows;
    }}
    if (isVisible(tableEl) && getRowCells(tableEl).length) {{
      return [tableEl];
    }}
    return [];
  }};

  const extractTable = (tableEl, headingEl = null) => {{
    const rows = getRows(tableEl);
    if (!rows.length) {{
      return null;
    }}

    let headerCells = [];
    let dataRows = rows.slice();
    for (const row of rows) {{
      const cells = getRowCells(row);
      const explicitHeaders = cells.filter((cell) =>
        (cell.getAttribute && cell.getAttribute("role") === "columnheader") ||
        String(cell.tagName || "").toLowerCase() === "th"
      );
      if (explicitHeaders.length) {{
        headerCells = explicitHeaders;
        dataRows = rows.filter((candidate) => candidate !== row);
        break;
      }}
    }}

    const visibleDataRows = dataRows.filter((row) => {{
      const values = getRowCells(row).map((cell) => textOf(cell.innerText || cell.textContent));
      return values.some(Boolean);
    }});

    const headerTexts = headerCells
      .map((cell) => textOf(cell.innerText || cell.textContent))
      .filter(Boolean);

    const maxCellCount = visibleDataRows.reduce((maxCount, row) => {{
      return Math.max(maxCount, getRowCells(row).length);
    }}, headerTexts.length);

    const headers = Array.from({{ length: maxCellCount }}, (_, index) =>
      headerTexts[index] || `col_${{index + 1}}`
    );

    const structuredRows = visibleDataRows.map((row) => {{
      const values = getRowCells(row)
        .map((cell) => textOf(cell.innerText || cell.textContent))
        .slice(0, headers.length);
      const mapped = {{}};
      headers.forEach((header, index) => {{
        mapped[header] = values[index] || "";
      }});
      return mapped;
    }});

    return {{
      success: true,
      heading_text: headingEl ? textOf(headingEl.innerText || headingEl.textContent) : null,
      headers,
      rows: structuredRows,
      row_count: structuredRows.length
    }};
  }};

  const candidateElements = Array.from(document.querySelectorAll("table, [role='table'], tbody, [role='rowgroup']"));
  const visibleCandidates = candidateElements.filter((element) => isVisible(element) && getRows(element).length);

  const evaluateCandidate = (tableEl, headingEl = null) => {{
    const extracted = extractTable(tableEl, headingEl);
    if (!extracted || !extracted.row_count) {{
      return null;
    }}
    return extracted;
  }};

  if (targetHeading) {{
    const headings = Array.from(document.querySelectorAll("h1, h2, h3, h4, h5, h6, [role='heading']"))
      .filter((heading) => isVisible(heading))
      .filter((heading) => normalize(heading.innerText || heading.textContent).includes(targetHeading));

    for (const heading of headings) {{
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
      let node = walker.currentNode;
      while (node && node !== heading) {{
        node = walker.nextNode();
      }}
      while ((node = walker.nextNode())) {{
        if (!(node instanceof Element) || !isVisible(node)) {{
          continue;
        }}
        const directMatch = (
          node.matches("table, [role='table'], tbody, [role='rowgroup']") ? node : null
        );
        const nestedMatch = directMatch || node.querySelector("table, [role='table'], tbody, [role='rowgroup']");
        const candidate = nestedMatch && isVisible(nestedMatch) ? nestedMatch : null;
        if (!candidate) {{
          continue;
        }}
        const extracted = evaluateCandidate(candidate, heading);
        if (extracted) {{
          return JSON.stringify(extracted);
        }}
      }}
    }}

    return JSON.stringify({{
      success: false,
      error: `No visible table found after heading "${{targetHeading}}".`
    }});
  }}

  let best = null;
  for (const candidate of visibleCandidates) {{
    const extracted = evaluateCandidate(candidate, null);
    if (!extracted) {{
      continue;
    }}
    if (!best || extracted.row_count > best.row_count) {{
      best = extracted;
    }}
  }}

  if (best) {{
    return JSON.stringify(best);
  }}

  return JSON.stringify({{
    success: false,
    error: "No visible table found on the current page."
  }});
}})()"""


# ============================================================================
# Browser Tool Functions
# ============================================================================

def browser_navigate(url: str, task_id: Optional[str] = None) -> str:
    """
    Navigate to a URL in the browser.
    
    Args:
        url: The URL to navigate to
        task_id: Task identifier for session isolation
        
    Returns:
        JSON string with navigation result (includes stealth features info on first nav)
    """
    # Website policy check — block before navigating
    blocked = check_website_access(url)
    if blocked:
        return json.dumps({
            "success": False,
            "error": blocked["message"],
            "blocked_by_policy": {"host": blocked["host"], "rule": blocked["rule"], "source": blocked["source"]},
        })

    effective_task_id = task_id or "default"
    
    # Get session info to check if this is a new session
    # (will create one with features logged if not exists)
    session_info = _get_session_info(effective_task_id)
    is_first_nav = session_info.get("_first_nav", True)
    
    # Auto-start recording if configured and this is first navigation
    if is_first_nav:
        session_info["_first_nav"] = False
        _maybe_start_recording(effective_task_id)
    
    result = _run_browser_command(effective_task_id, "open", [url], timeout=60)
    
    if result.get("success"):
        data = result.get("data", {})
        title = data.get("title", "")
        final_url = data.get("url", url)
        session_info["last_url"] = final_url
        
        response = {
            "success": True,
            "url": final_url,
            "title": title
        }
        
        # Detect common "blocked" page patterns from title/url
        blocked_patterns = [
            "access denied", "access to this page has been denied",
            "blocked", "bot detected", "verification required",
            "please verify", "are you a robot", "captcha",
            "cloudflare", "ddos protection", "checking your browser",
            "just a moment", "attention required"
        ]
        title_lower = title.lower()
        
        if any(pattern in title_lower for pattern in blocked_patterns):
            response["bot_detection_warning"] = (
                f"Page title '{title}' suggests bot detection. The site may have blocked this request. "
                "Options: 1) Try adding delays between actions, 2) Access different pages first, "
                "3) Enable advanced stealth (BROWSERBASE_ADVANCED_STEALTH=true, requires Scale plan), "
                "4) Some sites have very aggressive bot detection that may be unavoidable."
            )
        
        # Include feature info on first navigation so model knows what's active
        if is_first_nav and "features" in session_info:
            features = session_info["features"]
            active_features = [k for k, v in features.items() if v]
            if not features.get("proxies"):
                response["stealth_warning"] = (
                    "Running WITHOUT residential proxies. Bot detection may be more aggressive. "
                    "Consider upgrading Browserbase plan for proxy support."
                )
            response["stealth_features"] = active_features

        # Auto-snapshot: include compact page snapshot after successful navigation
        # so the model always receives element refs without a separate snapshot call.
        try:
            snap_result = _run_browser_command(effective_task_id, "snapshot", ["-c"])
            if snap_result.get("success"):
                snap_data = snap_result.get("data", {})
                snapshot_text = snap_data.get("snapshot", "")
                refs = snap_data.get("refs", {})
                if len(snapshot_text) > SNAPSHOT_SUMMARIZE_THRESHOLD:
                    snapshot_text = snapshot_text[:SNAPSHOT_SUMMARIZE_THRESHOLD] + "\n[...truncated]"
                response["snapshot"] = snapshot_text
                response["element_count"] = len(refs) if isinstance(refs, dict) else 0
        except Exception:
            pass  # Navigation succeeded even if snapshot fails.
        
        return json.dumps(response, ensure_ascii=False)
    else:
        return json.dumps({
            "success": False,
            "error": result.get("error", "Navigation failed")
        }, ensure_ascii=False)


def browser_snapshot(
    full: bool = False,
    task_id: Optional[str] = None,
    user_task: Optional[str] = None,
    stabilize: bool = False,
) -> str:
    """
    Get a text-based snapshot of the current page's accessibility tree.
    
    Args:
        full: If True, return complete snapshot. If False, return compact view.
        task_id: Task identifier for session isolation
        user_task: The user's current task (for task-aware extraction)
        stabilize: If True, retry over a short window and return the richest
                   successful snapshot
        
    Returns:
        JSON string with page snapshot
    """
    effective_task_id = task_id or "default"

    if not stabilize:
        return _build_snapshot_response(
            _run_snapshot_capture(effective_task_id, full),
            user_task=user_task,
        )

    attempts: List[tuple[int, Dict[str, Any]]] = []
    for attempt_index, delay_seconds in enumerate(SNAPSHOT_STABILIZE_DELAYS, start=1):
        if delay_seconds:
            time.sleep(delay_seconds)
        attempts.append((attempt_index, _run_snapshot_capture(effective_task_id, full)))

    best_success: Optional[tuple[int, Dict[str, Any], tuple[int, int]]] = None
    for attempt_index, result in attempts:
        if not result.get("success"):
            continue
        score = _score_snapshot_result(result)
        if best_success is None or score > best_success[2]:
            best_success = (attempt_index, result, score)

    if best_success is not None:
        selected_attempt, selected_result, _ = best_success
        return _build_snapshot_response(
            selected_result,
            user_task=user_task,
            stabilize=True,
            attempt_count=len(attempts),
            selected_attempt=selected_attempt,
        )

    last_result = attempts[-1][1]
    return _build_snapshot_response(
        last_result,
        user_task=user_task,
        stabilize=True,
        attempt_count=len(attempts),
        selected_attempt=None,
    )


def browser_click(ref: str, task_id: Optional[str] = None) -> str:
    """
    Click on an element.
    
    Args:
        ref: Element reference (e.g., "@e5")
        task_id: Task identifier for session isolation
        
    Returns:
        JSON string with click result
    """
    effective_task_id = task_id or "default"

    ref = _normalize_browser_ref(ref)

    snapshot_result = _get_compact_snapshot(effective_task_id)

    target_url = _resolve_click_navigation_url(
        effective_task_id,
        ref,
        snapshot_result=snapshot_result,
    )
    if target_url:
        navigation_result = browser_navigate(target_url, task_id=effective_task_id)
        try:
            navigation_data = json.loads(navigation_result)
        except json.JSONDecodeError:
            return navigation_result

        if navigation_data.get("success"):
            navigation_data["clicked"] = ref
            navigation_data["navigated"] = True

        return json.dumps(navigation_data, ensure_ascii=False)

    result = _run_browser_command(effective_task_id, "click", [ref])
    if not result.get("success") and _is_invalid_ref_or_selector_error(result.get("error")):
        snapshot_result = _get_compact_snapshot(effective_task_id)
        result = _run_browser_command(effective_task_id, "click", [ref])
    
    if result.get("success"):
        return json.dumps({
            "success": True,
            "clicked": ref
        }, ensure_ascii=False)
    else:
        return json.dumps({
            "success": False,
            "error": result.get("error", f"Failed to click {ref}")
        }, ensure_ascii=False)


def browser_select(
    ref: str,
    value: Optional[str] = None,
    option_ref: Optional[str] = None,
    task_id: Optional[str] = None,
) -> str:
    """
    Select an option in a dropdown.

    Args:
        ref: Select element reference (e.g., "@e5")
        value: Option label/value to select
        option_ref: Option element reference to resolve from the snapshot
        task_id: Task identifier for session isolation

    Returns:
        JSON string with select result
    """
    effective_task_id = task_id or "default"
    ref = _normalize_browser_ref(ref)

    has_value = bool(value and str(value).strip())
    has_option_ref = bool(option_ref and str(option_ref).strip())
    if has_value == has_option_ref:
        return json.dumps({
            "success": False,
            "error": "Provide exactly one of 'value' or 'option_ref'."
        }, ensure_ascii=False)

    snapshot_result = _get_compact_snapshot(effective_task_id)
    if not snapshot_result.get("success"):
        return json.dumps({
            "success": False,
            "error": snapshot_result.get("error", "Failed to hydrate browser snapshot")
        }, ensure_ascii=False)

    selected_value = str(value).strip() if has_value else ""
    if has_option_ref:
        selected_value = _resolve_select_option_value(
            effective_task_id,
            str(option_ref).strip(),
            snapshot_result=snapshot_result,
        ) or ""
    if selected_value:
        selected_value = _resolve_select_value_from_label(
            effective_task_id,
            ref,
            selected_value,
            snapshot_result=snapshot_result,
        )

    if not selected_value:
        return json.dumps({
            "success": False,
            "error": "Could not resolve the requested option from the current snapshot."
        }, ensure_ascii=False)

    snapshot_text = _snapshot_text(snapshot_result)
    wait_result: Optional[Dict[str, Any]] = None
    if snapshot_text.count("combobox") > 1:
        wait_result = _wait_for_select_option(
            effective_task_id,
            ref,
            selected_value,
            snapshot_result=snapshot_result,
        )
        matched_value = str(wait_result.get("matched_value") or "").strip()
        if matched_value:
            selected_value = matched_value
        elif wait_result.get("disabled") or int(wait_result.get("option_count") or 0) == 0:
            return json.dumps({
                "success": False,
                "error": (
                    f"Option {selected_value} is not available yet; "
                    "the dropdown is still disabled or its options have not loaded."
                ),
            }, ensure_ascii=False)

    dom_index = _resolve_select_dom_index(ref, snapshot_result=snapshot_result)
    if has_option_ref and wait_result and wait_result.get("ready") and dom_index not in (None, 0):
        eval_result = _select_via_eval(
            effective_task_id,
            ref,
            selected_value,
            snapshot_result=snapshot_result,
        )
        if eval_result.get("success"):
            return json.dumps({
                "success": True,
                "selected": selected_value,
                "element": ref,
            }, ensure_ascii=False)

    result = _run_browser_command(effective_task_id, "select", [ref, selected_value])
    fallback_snapshot = snapshot_result
    if not result.get("success") and (
        _is_invalid_ref_or_selector_error(result.get("error"))
        or _is_ambiguous_locator_error(result.get("error"))
    ):
        retry_snapshot = _get_compact_snapshot(effective_task_id)
        fallback_snapshot = retry_snapshot if retry_snapshot.get("success") else snapshot_result
        retry_ref = ref
        if _is_ambiguous_locator_error(result.get("error")):
            retry_ref = _resolve_combobox_selector(
                effective_task_id,
                ref,
                snapshot_result=retry_snapshot,
            ) or ref
        result = _run_browser_command(effective_task_id, "select", [retry_ref, selected_value])

    if not result.get("success") and _is_command_timeout_error(result.get("error")):
        result = _select_via_eval(
            effective_task_id,
            ref,
            selected_value,
            snapshot_result=fallback_snapshot,
        )

    if result.get("success"):
        return json.dumps({
            "success": True,
            "selected": selected_value,
            "element": ref,
        }, ensure_ascii=False)

    return json.dumps({
        "success": False,
        "error": result.get("error", f"Failed to select {selected_value} in {ref}")
    }, ensure_ascii=False)


def browser_click_row_detail(row_text: str, task_id: Optional[str] = None) -> str:
    """
    Click the rightmost interactive control in a table row matching row_text.

    Args:
        row_text: Text that identifies the row
        task_id: Task identifier for session isolation

    Returns:
        JSON string with click result
    """
    effective_task_id = task_id or "default"
    target_row = str(row_text or "").strip()
    if not target_row:
        return json.dumps({
            "success": False,
            "error": "row_text is required."
        }, ensure_ascii=False)

    script = _build_row_detail_click_script(target_row)
    result = _run_browser_command(effective_task_id, "eval", [script])
    if not result.get("success"):
        return json.dumps({
            "success": False,
            "error": result.get("error", f"Failed to click detail for row {target_row}")
        }, ensure_ascii=False)

    parsed = _parse_eval_result_dict(result.get("data", {}).get("result"))
    if parsed and parsed.get("success"):
        return json.dumps(parsed, ensure_ascii=False)

    return json.dumps({
        "success": False,
        "error": (parsed or {}).get("error", f"Failed to click detail for row {target_row}")
    }, ensure_ascii=False)


def browser_extract_visible_table(
    heading_text: Optional[str] = None,
    task_id: Optional[str] = None,
) -> str:
    """
    Extract a visible table into structured headers and rows.

    Args:
        heading_text: Optional heading text used to locate the target table
        task_id: Task identifier for session isolation

    Returns:
        JSON string with structured table data
    """
    effective_task_id = task_id or "default"
    script = _build_visible_table_extract_script(heading_text=heading_text)
    result = _run_browser_command(effective_task_id, "eval", [script])
    if not result.get("success"):
        return json.dumps({
            "success": False,
            "error": result.get("error", "Failed to extract visible table")
        }, ensure_ascii=False)

    parsed = _parse_eval_result_dict(result.get("data", {}).get("result"))
    if parsed and parsed.get("success"):
        return json.dumps(parsed, ensure_ascii=False)

    return json.dumps({
        "success": False,
        "error": (parsed or {}).get("error", "Failed to extract visible table")
    }, ensure_ascii=False)


def _resolve_browser_type_input(
    text: Optional[str],
    secret_env_var: Optional[str],
) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Resolve browser_type input, optionally from an environment variable."""
    provided_text = text if text is not None else None
    provided_secret = (secret_env_var or "").strip() or None

    if bool(provided_text is not None) == bool(provided_secret):
        return None, {
            "success": False,
            "error": "Provide exactly one of 'text' or 'secret_env_var'",
        }

    if provided_secret:
        if not _ENV_VAR_NAME_RE.match(provided_secret):
            return None, {
                "success": False,
                "error": f"Invalid environment variable name: {provided_secret}",
            }
        if provided_secret not in os.environ:
            return None, {
                "success": False,
                "error": f"Environment variable '{provided_secret}' is not set",
            }
        return os.environ[provided_secret], None

    return provided_text or "", None


def browser_type(
    ref: str,
    text: Optional[str] = None,
    secret_env_var: Optional[str] = None,
    task_id: Optional[str] = None,
) -> str:
    """
    Type text into an input field.
    
    Args:
        ref: Element reference (e.g., "@e3")
        text: Text to type
        secret_env_var: Optional environment variable name for sensitive values
        task_id: Task identifier for session isolation
        
    Returns:
        JSON string with type result
    """
    effective_task_id = task_id or "default"
    resolved_text, validation_error = _resolve_browser_type_input(text, secret_env_var)
    if validation_error:
        return json.dumps(validation_error, ensure_ascii=False)
    
    # Ensure ref starts with @
    if not ref.startswith("@"):
        ref = f"@{ref}"

    typed_text = resolved_text or ""
    result = _run_browser_command(effective_task_id, "fill", [ref, typed_text])
    fallback_snapshot: Optional[Dict[str, Any]] = None
    if not result.get("success") and (
        _is_invalid_ref_or_selector_error(result.get("error"))
        or _is_command_timeout_error(result.get("error"))
    ):
        retry_snapshot = _get_compact_snapshot(effective_task_id)
        fallback_snapshot = retry_snapshot if retry_snapshot.get("success") else None
        result = _run_browser_command(effective_task_id, "fill", [ref, typed_text])

        if not result.get("success"):
            result = _fill_via_eval(
                effective_task_id,
                ref,
                typed_text,
                snapshot_result=fallback_snapshot,
            )

    if result.get("success"):
        response: Dict[str, Any] = {
            "success": True,
            "typed": True,
            "typed_chars": len(typed_text),
            "element": ref,
        }
        if secret_env_var:
            response["typed_from_env"] = secret_env_var
        return json.dumps(response, ensure_ascii=False)
    else:
        return json.dumps({
            "success": False,
            "error": result.get("error", f"Failed to type into {ref}")
        }, ensure_ascii=False)


def browser_scroll(direction: str, task_id: Optional[str] = None) -> str:
    """
    Scroll the page.
    
    Args:
        direction: "up" or "down"
        task_id: Task identifier for session isolation
        
    Returns:
        JSON string with scroll result
    """
    effective_task_id = task_id or "default"
    
    # Validate direction
    if direction not in ["up", "down"]:
        return json.dumps({
            "success": False,
            "error": f"Invalid direction '{direction}'. Use 'up' or 'down'."
        }, ensure_ascii=False)
    
    result = _run_browser_command(effective_task_id, "scroll", [direction])
    
    if result.get("success"):
        return json.dumps({
            "success": True,
            "scrolled": direction
        }, ensure_ascii=False)
    else:
        return json.dumps({
            "success": False,
            "error": result.get("error", f"Failed to scroll {direction}")
        }, ensure_ascii=False)


def browser_back(task_id: Optional[str] = None) -> str:
    """
    Navigate back in browser history.
    
    Args:
        task_id: Task identifier for session isolation
        
    Returns:
        JSON string with navigation result
    """
    effective_task_id = task_id or "default"
    result = _run_browser_command(effective_task_id, "back", [])
    
    if result.get("success"):
        data = result.get("data", {})
        return json.dumps({
            "success": True,
            "url": data.get("url", "")
        }, ensure_ascii=False)
    else:
        return json.dumps({
            "success": False,
            "error": result.get("error", "Failed to go back")
        }, ensure_ascii=False)


def browser_press(key: str, task_id: Optional[str] = None) -> str:
    """
    Press a keyboard key.
    
    Args:
        key: Key to press (e.g., "Enter", "Tab")
        task_id: Task identifier for session isolation
        
    Returns:
        JSON string with key press result
    """
    effective_task_id = task_id or "default"
    result = _run_browser_command(effective_task_id, "press", [key])
    
    if result.get("success"):
        return json.dumps({
            "success": True,
            "pressed": key
        }, ensure_ascii=False)
    else:
        return json.dumps({
            "success": False,
            "error": result.get("error", f"Failed to press {key}")
        }, ensure_ascii=False)


def browser_close(task_id: Optional[str] = None) -> str:
    """
    Close the browser session.

    Args:
        task_id: Task identifier for session isolation

    Returns:
        JSON string with close result
    """
    effective_task_id = task_id or "default"
    with _cleanup_lock:
        had_session = effective_task_id in _active_sessions

    cleanup_browser(effective_task_id)

    response = {
        "success": True,
        "closed": True,
    }
    if not had_session:
        response["warning"] = "Session may not have been active"
    return json.dumps(response, ensure_ascii=False)


def browser_console(clear: bool = False, task_id: Optional[str] = None) -> str:
    """Get browser console messages and JavaScript errors.
    
    Returns both console output (log/warn/error/info from the page's JS)
    and uncaught exceptions (crashes, unhandled promise rejections).
    
    Args:
        clear: If True, clear the message/error buffers after reading
        task_id: Task identifier for session isolation
        
    Returns:
        JSON string with console messages and JS errors
    """
    effective_task_id = task_id or "default"
    
    console_args = ["--clear"] if clear else []
    error_args = ["--clear"] if clear else []
    
    console_result = _run_browser_command(effective_task_id, "console", console_args)
    errors_result = _run_browser_command(effective_task_id, "errors", error_args)
    
    messages = []
    if console_result.get("success"):
        for msg in console_result.get("data", {}).get("messages", []):
            messages.append({
                "type": msg.get("type", "log"),
                "text": msg.get("text", ""),
                "source": "console",
            })
    
    errors = []
    if errors_result.get("success"):
        for err in errors_result.get("data", {}).get("errors", []):
            errors.append({
                "message": err.get("message", ""),
                "source": "exception",
            })
    
    return json.dumps({
        "success": True,
        "console_messages": messages,
        "js_errors": errors,
        "total_messages": len(messages),
        "total_errors": len(errors),
    }, ensure_ascii=False)


def _maybe_start_recording(task_id: str):
    """Start recording if browser.record_sessions is enabled in config."""
    if task_id in _recording_sessions:
        return
    try:
        hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        config_path = hermes_home / "config.yaml"
        record_enabled = False
        if config_path.exists():
            import yaml
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            record_enabled = cfg.get("browser", {}).get("record_sessions", False)
        
        if not record_enabled:
            return
        
        recordings_dir = hermes_home / "browser_recordings"
        recordings_dir.mkdir(parents=True, exist_ok=True)
        _cleanup_old_recordings(max_age_hours=72)
        
        import time
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        recording_path = recordings_dir / f"session_{timestamp}_{task_id[:16]}.webm"
        
        result = _run_browser_command(task_id, "record", ["start", str(recording_path)])
        if result.get("success"):
            _recording_sessions.add(task_id)
            logger.info("Auto-recording browser session %s to %s", task_id, recording_path)
        else:
            logger.debug("Could not start auto-recording: %s", result.get("error"))
    except Exception as e:
        logger.debug("Auto-recording setup failed: %s", e)


def _maybe_stop_recording(task_id: str):
    """Stop recording if one is active for this session."""
    if task_id not in _recording_sessions:
        return
    try:
        result = _run_browser_command(task_id, "record", ["stop"])
        if result.get("success"):
            path = result.get("data", {}).get("path", "")
            logger.info("Saved browser recording for session %s: %s", task_id, path)
    except Exception as e:
        logger.debug("Could not stop recording for %s: %s", task_id, e)
    finally:
        _recording_sessions.discard(task_id)


def browser_get_images(task_id: Optional[str] = None) -> str:
    """
    Get all images on the current page.
    
    Args:
        task_id: Task identifier for session isolation
        
    Returns:
        JSON string with list of images (src and alt)
    """
    effective_task_id = task_id or "default"
    
    # Use eval to run JavaScript that extracts images
    js_code = """JSON.stringify(
        [...document.images].map(img => ({
            src: img.src,
            alt: img.alt || '',
            width: img.naturalWidth,
            height: img.naturalHeight
        })).filter(img => img.src && !img.src.startsWith('data:'))
    )"""
    
    result = _run_browser_command(effective_task_id, "eval", [js_code])
    
    if result.get("success"):
        data = result.get("data", {})
        raw_result = data.get("result", "[]")
        
        try:
            # Parse the JSON string returned by JavaScript
            if isinstance(raw_result, str):
                images = json.loads(raw_result)
            else:
                images = raw_result
            
            return json.dumps({
                "success": True,
                "images": images,
                "count": len(images)
            }, ensure_ascii=False)
        except json.JSONDecodeError:
            return json.dumps({
                "success": True,
                "images": [],
                "count": 0,
                "warning": "Could not parse image data"
            }, ensure_ascii=False)
    else:
        return json.dumps({
            "success": False,
            "error": result.get("error", "Failed to get images")
        }, ensure_ascii=False)


def browser_vision(question: str, annotate: bool = False, task_id: Optional[str] = None) -> str:
    """
    Take a screenshot of the current page and analyze it with vision AI.
    
    This tool captures what's visually displayed in the browser and sends it
    to Gemini for analysis. Useful for understanding visual content that the
    text-based snapshot may not capture (CAPTCHAs, verification challenges,
    images, complex layouts, etc.).
    
    The screenshot is saved persistently and its file path is returned alongside
    the analysis, so it can be shared with users via MEDIA:<path> in the response.
    
    Args:
        question: What you want to know about the page visually
        annotate: If True, overlay numbered [N] labels on interactive elements
        task_id: Task identifier for session isolation
        
    Returns:
        JSON string with vision analysis results and screenshot_path
    """
    import base64
    import uuid as uuid_mod
    from pathlib import Path
    
    effective_task_id = task_id or "default"
    
    # Save screenshot to persistent location so it can be shared with users
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    screenshots_dir = hermes_home / "browser_screenshots"
    screenshot_path = screenshots_dir / f"browser_screenshot_{uuid_mod.uuid4().hex}.png"
    
    try:
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        
        # Prune old screenshots (older than 24 hours) to prevent unbounded disk growth
        _cleanup_old_screenshots(screenshots_dir, max_age_hours=24)
        
        # Take screenshot using agent-browser
        screenshot_args = []
        if annotate:
            screenshot_args.append("--annotate")
        screenshot_args.append("--full")
        screenshot_args.append(str(screenshot_path))
        result = _run_browser_command(
            effective_task_id, 
            "screenshot", 
            screenshot_args,
            timeout=30
        )
        
        if not result.get("success"):
            error_detail = result.get("error", "Unknown error")
            _cp = _get_cloud_provider()
            mode = "local" if _cp is None else f"cloud ({_cp.provider_name()})"
            return json.dumps({
                "success": False,
                "error": f"Failed to take screenshot ({mode} mode): {error_detail}"
            }, ensure_ascii=False)

        actual_screenshot_path = result.get("data", {}).get("path")
        if actual_screenshot_path:
            screenshot_path = Path(actual_screenshot_path)

        # Check if screenshot file was created
        if not screenshot_path.exists():
            _cp = _get_cloud_provider()
            mode = "local" if _cp is None else f"cloud ({_cp.provider_name()})"
            return json.dumps({
                "success": False,
                "error": (
                    f"Screenshot file was not created at {screenshot_path} ({mode} mode). "
                    f"This may indicate a socket path issue (macOS /var/folders/), "
                    f"a missing Chromium install ('agent-browser install'), "
                    f"or a stale daemon process."
                ),
            }, ensure_ascii=False)
        
        # Read and convert to base64
        image_data = screenshot_path.read_bytes()
        image_base64 = base64.b64encode(image_data).decode("ascii")
        data_url = f"data:image/png;base64,{image_base64}"
        
        vision_prompt = (
            f"You are analyzing a screenshot of a web browser.\n\n"
            f"User's question: {question}\n\n"
            f"Provide a detailed and helpful answer based on what you see in the screenshot. "
            f"If there are interactive elements, describe them. If there are verification challenges "
            f"or CAPTCHAs, describe what type they are and what action might be needed. "
            f"Focus on answering the user's specific question."
        )

        # Use the centralized LLM router
        vision_model = _get_vision_model()
        logger.debug("browser_vision: analysing screenshot (%d bytes)",
                     len(image_data))
        call_kwargs = {
            "task": "vision",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": vision_prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "max_tokens": 2000,
            "temperature": 0.1,
        }
        if vision_model:
            call_kwargs["model"] = vision_model
        response = call_llm(**call_kwargs)
        
        analysis = response.choices[0].message.content
        response_data = {
            "success": True,
            "analysis": analysis,
            "screenshot_path": str(screenshot_path),
        }
        # Include annotation data if annotated screenshot was taken
        if annotate and result.get("data", {}).get("annotations"):
            response_data["annotations"] = result["data"]["annotations"]
        return json.dumps(response_data, ensure_ascii=False)
    
    except Exception as e:
        # Keep the screenshot if it was captured successfully — the failure is
        # in the LLM vision analysis, not the capture.  Deleting a valid
        # screenshot loses evidence the user might need.  The 24-hour cleanup
        # in _cleanup_old_screenshots prevents unbounded disk growth.
        logger.warning("browser_vision failed: %s", e, exc_info=True)
        error_info = {"success": False, "error": f"Error during vision analysis: {str(e)}"}
        if screenshot_path.exists():
            error_info["screenshot_path"] = str(screenshot_path)
            error_info["note"] = "Screenshot was captured but vision analysis failed. You can still share it via MEDIA:<path>."
        return json.dumps(error_info, ensure_ascii=False)


def _cleanup_old_screenshots(screenshots_dir, max_age_hours=24):
    """Remove browser screenshots older than max_age_hours to prevent disk bloat.

    Throttled to run at most once per hour per directory to avoid repeated
    scans on screenshot-heavy workflows.
    """
    key = str(screenshots_dir)
    now = time.time()
    if now - _last_screenshot_cleanup_by_dir.get(key, 0.0) < 3600:
        return
    _last_screenshot_cleanup_by_dir[key] = now

    try:
        cutoff = time.time() - (max_age_hours * 3600)
        for f in screenshots_dir.glob("browser_screenshot_*.png"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception as e:
                logger.debug("Failed to clean old screenshot %s: %s", f, e)
    except Exception as e:
        logger.debug("Screenshot cleanup error (non-critical): %s", e)


def _cleanup_old_recordings(max_age_hours=72):
    """Remove browser recordings older than max_age_hours to prevent disk bloat."""
    import time
    try:
        hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        recordings_dir = hermes_home / "browser_recordings"
        if not recordings_dir.exists():
            return
        cutoff = time.time() - (max_age_hours * 3600)
        for f in recordings_dir.glob("session_*.webm"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception as e:
                logger.debug("Failed to clean old recording %s: %s", f, e)
    except Exception as e:
        logger.debug("Recording cleanup error (non-critical): %s", e)


# ============================================================================
# Cleanup and Management Functions
# ============================================================================

def cleanup_browser(task_id: Optional[str] = None) -> None:
    """
    Clean up browser session for a task.
    
    Called automatically when a task completes or when inactivity timeout is reached.
    Closes both the agent-browser session and the Browserbase session.
    
    Args:
        task_id: Task identifier to clean up
    """
    if task_id is None:
        task_id = "default"
    
    logger.debug("cleanup_browser called for task_id: %s", task_id)
    logger.debug("Active sessions: %s", list(_active_sessions.keys()))
    
    # Check if session exists (under lock), but don't remove yet -
    # _run_browser_command needs it to build the close command.
    with _cleanup_lock:
        session_info = _active_sessions.get(task_id)
    
    if session_info:
        bb_session_id = session_info.get("bb_session_id", "unknown")
        logger.debug("Found session for task %s: bb_session_id=%s", task_id, bb_session_id)
        
        # Stop auto-recording before closing (saves the file)
        _maybe_stop_recording(task_id)
        
        # Try to close via agent-browser first (needs session in _active_sessions)
        try:
            _run_browser_command(task_id, "close", [], timeout=10)
            logger.debug("agent-browser close command completed for task %s", task_id)
        except Exception as e:
            logger.warning("agent-browser close failed for task %s: %s", task_id, e)
        
        # Now remove from tracking under lock
        with _cleanup_lock:
            _active_sessions.pop(task_id, None)
            _session_last_activity.pop(task_id, None)
        
        # Cloud mode: close the cloud browser session via provider API
        if bb_session_id:
            provider = _get_cloud_provider()
            if provider is not None:
                try:
                    provider.close_session(bb_session_id)
                except Exception as e:
                    logger.warning("Could not close cloud browser session: %s", e)
        
        # Kill the daemon process and clean up socket directory
        session_name = session_info.get("session_name", "")
        if session_name:
            socket_dir = os.path.join(_socket_safe_tmpdir(), f"agent-browser-{session_name}")
            if os.path.exists(socket_dir):
                # agent-browser writes {session}.pid in the socket dir
                pid_file = os.path.join(socket_dir, f"{session_name}.pid")
                if os.path.isfile(pid_file):
                    try:
                        daemon_pid = int(Path(pid_file).read_text().strip())
                        os.kill(daemon_pid, signal.SIGTERM)
                        logger.debug("Killed daemon pid %s for %s", daemon_pid, session_name)
                    except (ProcessLookupError, ValueError, PermissionError, OSError):
                        logger.debug("Could not kill daemon pid for %s (already dead or inaccessible)", session_name)
                shutil.rmtree(socket_dir, ignore_errors=True)
        
        logger.debug("Removed task %s from active sessions", task_id)
    else:
        logger.debug("No active session found for task_id: %s", task_id)


def cleanup_all_browsers() -> None:
    """
    Clean up all active browser sessions.
    
    Useful for cleanup on shutdown.
    """
    with _cleanup_lock:
        task_ids = list(_active_sessions.keys())
    for task_id in task_ids:
        cleanup_browser(task_id)


def get_active_browser_sessions() -> Dict[str, Dict[str, str]]:
    """
    Get information about active browser sessions.
    
    Returns:
        Dict mapping task_id to session info (session_name, bb_session_id, cdp_url)
    """
    with _cleanup_lock:
        return _active_sessions.copy()


# ============================================================================
# Requirements Check
# ============================================================================

def check_browser_requirements() -> bool:
    """
    Check if browser tool requirements are met.

    In **local mode** (no Browserbase credentials): only the ``agent-browser``
    CLI must be findable.

    In **cloud mode** (BROWSERBASE_API_KEY set): the CLI *and* both
    ``BROWSERBASE_API_KEY`` / ``BROWSERBASE_PROJECT_ID`` must be present.
    
    Returns:
        True if all requirements are met, False otherwise
    """
    # The agent-browser CLI is always required
    try:
        _find_agent_browser()
    except FileNotFoundError:
        return False

    # In cloud mode, also require provider credentials
    provider = _get_cloud_provider()
    if provider is not None and not provider.is_configured():
        return False

    return True


# ============================================================================
# Module Test
# ============================================================================

if __name__ == "__main__":
    """
    Simple test/demo when run directly
    """
    print("🌐 Browser Tool Module")
    print("=" * 40)

    _cp = _get_cloud_provider()
    mode = "local" if _cp is None else f"cloud ({_cp.provider_name()})"
    print(f"   Mode: {mode}")
    
    # Check requirements
    if check_browser_requirements():
        print("✅ All requirements met")
    else:
        print("❌ Missing requirements:")
        try:
            _find_agent_browser()
        except FileNotFoundError:
            print("   - agent-browser CLI not found")
            print("     Install: npm install -g agent-browser && agent-browser install --with-deps")
        if _cp is not None and not _cp.is_configured():
            print(f"   - {_cp.provider_name()} credentials not configured")
            print("   Tip: remove cloud_provider from config to use free local mode instead")
    
    print("\n📋 Available Browser Tools:")
    for schema in BROWSER_TOOL_SCHEMAS:
        print(f"  🔹 {schema['name']}: {schema['description'][:60]}...")
    
    print("\n💡 Usage:")
    print("  from tools.browser_tool import browser_navigate, browser_snapshot")
    print("  result = browser_navigate('https://example.com', task_id='my_task')")
    print("  snapshot = browser_snapshot(task_id='my_task')")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
from tools.registry import registry

_BROWSER_SCHEMA_MAP = {s["name"]: s for s in BROWSER_TOOL_SCHEMAS}

registry.register(
    name="browser_navigate",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_navigate"],
    handler=lambda args, **kw: browser_navigate(url=args.get("url", ""), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="🌐",
)
registry.register(
    name="browser_snapshot",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_snapshot"],
    handler=lambda args, **kw: browser_snapshot(
        full=args.get("full", False),
        task_id=kw.get("task_id"),
        user_task=kw.get("user_task"),
        stabilize=args.get("stabilize", False),
    ),
    check_fn=check_browser_requirements,
    emoji="📸",
)
registry.register(
    name="browser_click",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_click"],
    handler=lambda args, **kw: browser_click(ref=args.get("ref", ""), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="👆",
)
registry.register(
    name="browser_select",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_select"],
    handler=lambda args, **kw: browser_select(
        ref=args.get("ref", ""),
        value=args.get("value"),
        option_ref=args.get("option_ref"),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_browser_requirements,
    emoji="🗂️",
)
registry.register(
    name="browser_click_row_detail",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_click_row_detail"],
    handler=lambda args, **kw: browser_click_row_detail(
        row_text=args.get("row_text", ""),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_browser_requirements,
    emoji="🔎",
)
registry.register(
    name="browser_extract_visible_table",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_extract_visible_table"],
    handler=lambda args, **kw: browser_extract_visible_table(
        heading_text=args.get("heading_text"),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_browser_requirements,
    emoji="📋",
)
registry.register(
    name="browser_type",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_type"],
    handler=lambda args, **kw: browser_type(
        ref=args.get("ref", ""),
        text=args.get("text"),
        secret_env_var=args.get("secret_env_var"),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_browser_requirements,
    emoji="⌨️",
)
registry.register(
    name="browser_scroll",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_scroll"],
    handler=lambda args, **kw: browser_scroll(direction=args.get("direction", "down"), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="📜",
)
registry.register(
    name="browser_back",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_back"],
    handler=lambda args, **kw: browser_back(task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="◀️",
)
registry.register(
    name="browser_press",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_press"],
    handler=lambda args, **kw: browser_press(key=args.get("key", ""), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="⌨️",
)
registry.register(
    name="browser_close",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_close"],
    handler=lambda args, **kw: browser_close(task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="🚪",
)
registry.register(
    name="browser_get_images",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_get_images"],
    handler=lambda args, **kw: browser_get_images(task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="🖼️",
)
registry.register(
    name="browser_vision",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_vision"],
    handler=lambda args, **kw: browser_vision(question=args.get("question", ""), annotate=args.get("annotate", False), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="👁️",
)
registry.register(
    name="browser_console",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_console"],
    handler=lambda args, **kw: browser_console(clear=args.get("clear", False), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="🖥️",
)
