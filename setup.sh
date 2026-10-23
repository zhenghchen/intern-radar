#!/usr/bin/env bash
# One-shot setup: creates the GitHub repo, pushes, sets the alert secret, turns on the board, starts the first runs.
# Usage (from inside the unzipped intern-radar folder):  bash setup.sh
set -euo pipefail
REPO="${1:-intern-radar}"
TOPIC="${NTFY_TOPIC:-zheng-radar-fdfbb4f139}"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

command -v git >/dev/null || { echo "git is not installed."; exit 1; }
if ! command -v gh >/dev/null; then
  echo "GitHub CLI (gh) is not installed."
  echo "  macOS:   brew install gh"
  echo "  Windows: winget install --id GitHub.cli   (then run this script in Git Bash)"
  echo "  Linux:   https://github.com/cli/cli#installation"
  exit 1
fi

step "Signing in to GitHub (a browser window may open)"
if ! gh auth status >/dev/null 2>&1; then
  gh auth login -h github.com -p https -w -s repo,workflow
elif ! gh auth status 2>&1 | grep -q "workflow"; then
  gh auth refresh -h github.com -s workflow
fi
gh auth setup-git >/dev/null
USER="$(gh api user -q .login)"
FULL="$USER/$REPO"
BOARD="https://$USER.github.io/$REPO/"
echo "GitHub user: $USER"

step "Creating public repo $FULL and pushing"
[ -d .git ] || git init -q -b main
git add -A
git -c user.name="$USER" -c user.email="$USER@users.noreply.github.com" commit -qm "Intern Radar" 2>/dev/null || true
if gh repo view "$FULL" >/dev/null 2>&1; then
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/$FULL.git"
  git push -u origin main
else
  gh repo create "$FULL" --public --description "My internship radar" --source . --remote origin --push
fi

step "Saving the phone-alert topic as a repo secret"
gh secret set NTFY_TOPIC -R "$FULL" -b "$TOPIC"
gh variable set BOARD_URL -R "$FULL" -b "$BOARD"

step "Turning on GitHub Pages for the board"
gh api -X POST "repos/$FULL/pages" -f build_type=workflow >/dev/null 2>&1 \
  || gh api -X PUT "repos/$FULL/pages" -f build_type=workflow >/dev/null 2>&1 || true

step "Starting the first runs"
sleep 3
gh workflow enable poll.yml -R "$FULL" >/dev/null 2>&1 || true
gh workflow enable discover.yml -R "$FULL" >/dev/null 2>&1 || true
gh workflow run pages.yml -R "$FULL"
gh workflow run poll.yml -R "$FULL"
gh workflow run discover.yml -R "$FULL"

step "Sending a test alert to ntfy topic $TOPIC"
curl -s -H "Title: Intern Radar is connected" -d "You'll get a ping here for each new matching role." "https://ntfy.sh/$TOPIC" >/dev/null && echo "Sent."

cat <<EOF

Done.
  Board:   $BOARD   (live in ~2 minutes; data appears after the first poll finishes, ~3-5 minutes)
  Runs:    https://github.com/$FULL/actions
  Repo:    https://github.com/$FULL

On your phone: install the ntfy app and subscribe to the topic:  $TOPIC
EOF
