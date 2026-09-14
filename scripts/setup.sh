#!/bin/bash
# ============================================================================
# Otto setup — one command, safe to re-run.
#
#   ./scripts/setup.sh                   interactive setup
#   ./scripts/setup.sh --key sk-...      also store an API key (optional)
#   ./scripts/setup.sh --no-launchd      don't install the run-at-login jobs
#   ./scripts/setup.sh --no-menubar      skip building the menu bar app
#   ./scripts/setup.sh --no-permissions  skip the macOS permission walk-through
#   ./scripts/setup.sh --no-slack        skip the optional Slack token step
#   ./scripts/setup.sh --dev             also install test tooling
#
# What it does (and nothing else):
#   1. checks macOS + Python 3.9+
#   2. creates ./.venv and installs Otto's two runtime dependencies
#   3. optionally stores an API key (0600 file in ~/Library/Application Support/Otto)
#   4. optionally connects a read-only Slack token (every channel and DM, not just the window)
#   5. builds the menu bar app if the Xcode command line tools are present
#   6. installs the launchd jobs so the engine (and menu bar app) start at login
#   7. asks macOS for the Automation / Accessibility / Screen Recording grants Otto needs
#   8. writes config.toml (every setting explained) and shows `otto status`
#
# It never edits your shell profile, never writes outside the project
# directory and ~/Library, and never restarts other apps.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

BOLD=$'\033[1m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; NC=$'\033[0m'
ok()   { echo "${GREEN}✓${NC} $*"; }
warn() { echo "${YELLOW}!${NC} $*"; }
fail() { echo "${RED}✗${NC} $*"; }
step() { echo; echo "${BOLD}$*${NC}"; }

API_KEY=""; WITH_LAUNCHD=1; WITH_MENUBAR=1; WITH_DEV=0; WITH_PERMS=1; WITH_SLACK=1
while [ $# -gt 0 ]; do
  case "$1" in
    --key) API_KEY="$2"; shift 2 ;;
    --no-launchd) WITH_LAUNCHD=0; shift ;;
    --no-menubar) WITH_MENUBAR=0; shift ;;
    --no-permissions) WITH_PERMS=0; shift ;;
    --no-slack) WITH_SLACK=0; shift ;;
    --dev) WITH_DEV=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) fail "unknown option: $1"; exit 2 ;;
  esac
done

# --- 1. platform -----------------------------------------------------------
step "1/8  System"
if [ "$(uname)" != "Darwin" ]; then
  fail "Otto only runs on macOS (it reads Slack.app and browsers via macOS automation)."; exit 1
fi
ok "macOS $(sw_vers -productVersion)"

PYTHON=""
for py in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
  if command -v "$py" >/dev/null 2>&1; then
    if "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      PYTHON="$(command -v "$py")"; break
    fi
  fi
done
if [ -z "$PYTHON" ]; then
  fail "Python 3.9+ not found. macOS ships one with the Xcode command line tools:  xcode-select --install"; exit 1
fi
ok "Python $("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')  ($PYTHON)"

# --- 2. virtualenv + dependencies -------------------------------------------
step "2/8  Python environment (.venv)"
if [ ! -x .venv/bin/python3 ]; then
  "$PYTHON" -m venv .venv
  ok "created .venv"
else
  ok ".venv already exists"
fi
VENV_PY="$PROJECT_DIR/.venv/bin/python3"
"$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
EXTRAS=""; [ "$WITH_DEV" = 1 ] && EXTRAS="[dev]"
if "$VENV_PY" -m pip install --quiet -e ".${EXTRAS}" 2>/dev/null; then
  ok "installed otto${EXTRAS} into .venv"
else
  warn "editable install failed — installing runtime dependencies directly"
  "$VENV_PY" -m pip install --quiet httpx aiosqlite 'tomli; python_version < "3.11"'
  [ "$WITH_DEV" = 1 ] && "$VENV_PY" -m pip install --quiet pytest pytest-asyncio ruff
  ok "runtime dependencies installed"
fi
OTTO="$PROJECT_DIR/otto"
chmod +x "$OTTO"

# --- 3. API key (optional) ---------------------------------------------------
step "3/8  AI (optional)"
if [ -n "$API_KEY" ]; then
  "$OTTO" key add "$API_KEY" && ok "key stored (0600, outside the repo)"
elif ! "$OTTO" key list 2>/dev/null | grep -q "No API keys configured"; then
  ok "an API key is already configured"
elif [ -t 0 ]; then
  echo "  Otto works without a key (local heuristics). A key from OpenRouter, OpenAI,"
  echo "  Anthropic, Gemini or Devin adds LLM summaries and better prioritisation."
  printf "  Paste a key now, or press Enter to skip: "
  read -r -s KEY_INPUT; echo
  if [ -n "$KEY_INPUT" ]; then
    "$OTTO" key add "$KEY_INPUT" && ok "key stored (0600, outside the repo)"
  else
    ok "skipped — add one later with:  ./otto key add <key>  (or under [keys] in config.toml)"
  fi
else
  ok "no key configured — heuristic mode (add later with ./otto key add <key>, or under [keys] in config.toml)"
fi

# --- 4. Slack token (optional) ----------------------------------------------
step "4/8  Slack (optional)"
if [ "$WITH_SLACK" = 0 ]; then
  ok "skipped (--no-slack) — later: ./otto slack connect"
elif "$OTTO" key list 2>/dev/null | grep -q "slack, read-only"; then
  ok "a Slack token is already configured (./otto status shows what it reads)"
elif [ -t 0 ]; then
  echo "  Without a token Otto reads what Slack.app has on screen — every open window, and the"
  echo "  sidebar for what is unread. A read-only user token reads every channel, DM and thread"
  echo "  you are in, every minute, opened or not."
  echo "  Connecting opens Slack's create-app page in your browser with the manifest filled in;"
  echo "  you click Create, Install, Allow, and paste the token here. About two minutes."
  printf "  Connect Slack now? [y/N] "
  read -r SLACK_YN
  case "$SLACK_YN" in
    y|Y|yes) "$OTTO" slack connect || warn "not connected — try again later with ./otto slack connect" ;;
    *) ok "skipped — later: ./otto slack connect" ;;
  esac
else
  ok "no Slack token — window reading only (later: ./otto slack connect)"
fi

# --- 5. menu bar app ---------------------------------------------------------
step "5/8  Menu bar app"
if [ "$WITH_MENUBAR" = 1 ]; then
  if command -v swiftc >/dev/null 2>&1; then
    BUILD_LOG="$(mktemp -t otto-build)"
    if "$SCRIPT_DIR/build_menubar.sh" --python "$VENV_PY" >"$BUILD_LOG" 2>&1; then
      ok "built bin/Otto.app"
      rm -f "$BUILD_LOG"
    else
      warn "build failed — Otto still works from the browser (./otto open); compiler output: $BUILD_LOG"
    fi
  else
    warn "swiftc not found; skipping (install with: xcode-select --install, then scripts/build_menubar.sh)"
  fi
else
  ok "skipped (--no-menubar)"
fi

# --- 6. run at login ---------------------------------------------------------
step "6/8  Run at login (launchd)"
if [ "$WITH_LAUNCHD" = 1 ]; then
  if "$OTTO" install; then
    ok "Otto starts at login and restarts if it crashes"
  else
    warn "launchd install failed — start manually with: ./otto start"
  fi
else
  "$OTTO" start || true
  ok "started in the background (not installed at login: --no-launchd)"
  if [ -x bin/Otto.app/Contents/MacOS/OttoMenuBar ] && ! pgrep -x OttoMenuBar >/dev/null 2>&1; then
    open bin/Otto.app 2>/dev/null || true
  fi
fi

# --- 7. permissions ----------------------------------------------------------
step "7/8  macOS permissions"
if [ "$WITH_PERMS" = 1 ]; then
  echo "  Otto reads Slack and browser tabs through macOS automation. macOS will ask once;"
  echo "  everything Otto does is read-only."
  "$OTTO" permissions || true
else
  ok "skipped (--no-permissions) — run ./otto permissions later"
fi

# --- 8. settings + check -------------------------------------------------------
step "8/8  Settings and a first look"
# config.toml holds everything that used to be a flag (port, refresh interval,
# who you are, standing directives, keys…). Written once, with every setting
# explained; the engine re-reads it within a minute of a save.
PYTHONPATH="$PROJECT_DIR/src" OTTO_HOME="$PROJECT_DIR" "$VENV_PY" - <<'PY' || true
from otto.config import ensure_config_file
print(f"  settings: {ensure_config_file()}   (edit any time: the menu bar's Edit Config…, or ./otto config)")
PY
sleep 2
"$OTTO" status || true

PORT="$(PYTHONPATH="$PROJECT_DIR/src" OTTO_HOME="$PROJECT_DIR" "$VENV_PY" -c 'from otto.cli import _port; print(_port())' 2>/dev/null || echo "${OTTO_PORT:-7077}")"
echo
echo "${BOLD}Done.${NC}  Everything is in the menu bar (click the icon). Briefing page: http://localhost:${PORT}"
echo "       Terminal, when you need it:  ./otto status   ·   ./otto config   ·   ./otto --help"
open "http://localhost:${PORT}" 2>/dev/null || true
