#!/usr/bin/env bash
# Regenerates docs/llms-full.txt by concatenating all docs pages in nav order.
# Run from repo root. Invoked by .github/workflows/docs.yml before mkdocs build.
set -euo pipefail

cd "$(dirname "$0")/.."

OUT="docs/llms-full.txt"
BASE_URL="https://nobox.evilsocket.net"

{
  printf '# nobox - full documentation\n\n'
  printf 'Single-file concatenation of every page under https://nobox.evilsocket.net/, intended for ingestion by LLMs and AI agents. Source: https://github.com/evilsocket/nobox/tree/main/docs\n\n'
  printf 'Generated: %s\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf -- '---\n\n'
} > "$OUT"

emit() {
  local path="$1"
  local relpath="${path#docs/}"
  local url_path="${relpath%.md}"
  if [[ "$url_path" == "index" ]]; then
    url="$BASE_URL/"
  else
    url="$BASE_URL/$url_path/"
  fi
  {
    printf '\n\n## Source: %s\n\n' "$url"
    cat "$path"
    printf '\n\n---\n'
  } >> "$OUT"
}

# Order mirrors mkdocs.yml nav.
pages=(
  docs/index.md
  docs/install.md
  docs/usage.md
  docs/how-it-works.md
  docs/mcp.md
  docs/skill.md
  docs/pgp.md
  docs/imap.md
)

for p in "${pages[@]}"; do
  if [[ -f "$p" ]]; then
    emit "$p"
  else
    echo "warn: $p missing, skipping" >&2
  fi
done

echo "wrote $OUT ($(wc -l < "$OUT") lines, $(wc -c < "$OUT") bytes)"
