#!/bin/bash
set -ueo pipefail

MYDIR="$(realpath "$(dirname -- "${BASH_SOURCE[0]}")")"
FILES=("$MYDIR"/{browse-tui,md,md2ansi,recipes/{browse-claude,browse-find,browse-fs,browse-git,browse-jira,browse-jira-mcp,browse-mcp,browse-md,browse-plan,browse-procs,md2ansi_lib.py,md_doc.py}})
DESTS=("$HOME/.local/bin" "$HOME/bin" "/usr/local/bin")

relpath() { # args: from, to
  python3 -c 'import os,sys; print(os.path.relpath(sys.argv[2], sys.argv[1]))' "$@"
}

install() {
  for DEST in "${DESTS[@]}"; do
    [[ ! -d "$DEST" ]] && continue
    cd "$DEST" || continue
    echo "Installing into $DEST ..." 1>&2
    for FILE in "${FILES[@]}"; do
      printf ' %q' "$@" "$(relpath "$DEST" "$FILE")" ./ 1>&2; echo 1>&2
      "$@" "$(relpath "$DEST" "$FILE")" ./
    done
    [[ ":$PATH:" == *":$DEST:"* ]] || echo "NOTE: $DEST is not in your \$PATH." 1>&2
    return 0
  done
  echo "ERROR: None of install destination directories exist - create one for installation:" 1>&2
  for DEST in "${DESTS[@]}"; do
    echo "  $DEST" 1>&2
  done
  return 1
}

is_my_file() {
  grep -qsF browse-tui "$1" || [[ "$(readlink "$1" 2>/dev/null)" == *browse-tui* ]]
}

uninstall() {
  for DEST in "${DESTS[@]}"; do
    [[ ! -d "$DEST" ]] && continue
    for FILE in "${FILES[@]}"; do
      F="$DEST"/"$(basename "$FILE")"
      [[ ! -e "$F" && ! -L "$F" ]] && continue
      is_my_file "$F" || { echo " ... skipped: $F" 1>&2; continue; }
      printf ' %q' rm -f "$F" 1>&2; echo 1>&2
      rm -f "$F" || true
    done
  done
}

