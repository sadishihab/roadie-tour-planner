#!/bin/sh
# Container entrypoint: install the gallery files from the secrets folder, trust the Qloo
# hackathon endpoint, then start uvicorn (one worker).
#
# Prints counts only: never a file name (it could be Qloo data), never an environment value, and
# never the Qloo key. The key stays in the QLOO_API_KEY environment variable.
set -eu

DATA_DIR="${ROADIE_DATA_DIR:-/app/data}"
SECRETS_DIR="${ROADIE_SECRETS_DIR:-/etc/secrets}"
GALLERY_DIR="$DATA_DIR/gallery"
HACKATHON_URL="https://hackathon.api.qloo.com"

mkdir -p "$GALLERY_DIR"

copied=0
skipped=0
secrets_state=missing
if [ -d "$SECRETS_DIR" ]; then
  # Readable means both listable (r) and enterable (x) by the current user. Check by actually
  # listing, so a mount the user cannot read is told apart from a folder that is merely empty.
  if [ -r "$SECRETS_DIR" ] && [ -x "$SECRETS_DIR" ] && listing=$(ls -A "$SECRETS_DIR" 2>/dev/null); then
    secrets_state=empty
    [ -z "$listing" ] || secrets_state=readable
  else
    secrets_state=unreadable
  fi
fi
if [ "$secrets_state" = readable ]; then
  for src in "$SECRETS_DIR"/*; do
    [ -e "$src" ] || continue
    name=${src##*/}
    # Only <slug>.json, slug = [a-z0-9_]{1,40}; a regular file (never a folder or device).
    if printf '%s\n' "$name" | grep -Eq '^[a-z0-9_]{1,40}\.json$' && [ -f "$src" ]; then
      cp -- "$src" "$GALLERY_DIR/$name"   # a copy, never a symlink
      copied=$((copied + 1))
    else
      skipped=$((skipped + 1))
    fi
  done
fi
if [ "$secrets_state" = unreadable ]; then
  echo "roadie: the secrets folder exists but is not readable by this user; the gallery will be empty (check the folder permissions)"
elif [ "$copied" -gt 0 ]; then
  echo "roadie: gallery files copied: $copied (entries skipped: $skipped)"
elif [ "$secrets_state" = missing ]; then
  echo "roadie: no gallery files found (the secrets folder is missing); the gallery will be empty"
elif [ "$secrets_state" = empty ]; then
  echo "roadie: no gallery files found (the secrets folder is empty); the gallery will be empty"
else
  echo "roadie: no gallery files found; the gallery will be empty (entries skipped: $skipped)"
fi

# Trust the hackathon endpoint (the harness defaults to production and answers 401 otherwise).
# Defaults only: an operator-supplied value wins. The harness reads both variables itself.
QLOO_BASE_URL="${QLOO_BASE_URL:-$HACKATHON_URL}"
QLOO_TRUSTED_BASE_URL="${QLOO_TRUSTED_BASE_URL:-$HACKATHON_URL}"
export QLOO_BASE_URL QLOO_TRUSTED_BASE_URL
# Also record it in the harness config when the command exists. Run without the key in its
# environment and with output discarded; a failure is not fatal because the variables above suffice.
if command -v qloo >/dev/null 2>&1; then
  env -u QLOO_API_KEY qloo config set base-url "$QLOO_BASE_URL" >/dev/null 2>&1 \
    || echo "roadie: qloo config set base-url failed; relying on QLOO_BASE_URL and QLOO_TRUSTED_BASE_URL"
fi

PORT="${PORT:-8000}"
case "$PORT" in ''|*[!0-9]*) PORT=8000 ;; esac

exec python -m uvicorn roadie.api:app --host 0.0.0.0 --port "$PORT" --workers 1
