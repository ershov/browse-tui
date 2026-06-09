#!/bin/bash
set -ueo pipefail

MYDIR="$(realpath "$(dirname -- "${BASH_SOURCE[0]}")")"
FILES=("$MYDIR"/{browse-tui,md,md2ansi,recipes/{browse-claude,browse-find,browse-fs,browse-git,browse-jira,browse-jira-mcp,browse-mcp,browse-md,browse-plan,browse-procs,md2ansi_lib.py,md_doc.py}})
DESTS=("$HOME/.local/bin" "$HOME/bin" "/usr/local/bin")

relpath() { # args: from, to
  python3 -c 'import os,sys; print(os.path.relpath(sys.argv[2], sys.argv[1]))' "$@"
}

file_alias() {
  local FILE="$(basename "$1")"
  ALIAS="${FILE/browse-/b-}"
  [[ "$FILE" != "$ALIAS" ]]
}

install() {
  for DEST in "${DESTS[@]}"; do
    [[ ! -d "$DEST" ]] && continue
    cd "$DEST" || continue
    echo "Installing into $DEST ..." 1>&2
    for FILE in "${FILES[@]}"; do
      printf ' %q' "$@" "$(relpath "$DEST" "$FILE")" ./ 1>&2; echo 1>&2
      "$@" "$(relpath "$DEST" "$FILE")" ./
      if file_alias "$(basename "$FILE")"; then
        echo " symlink $ALIAS"
        (cd "$DEST"; ln -s "$FILE" "$ALIAS")
      fi
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

rm_my_file() {
  [[ ! -e "$1" && ! -L "$1" ]] && return 0
  is_my_file "$1" || { echo " ... skipped: $1" 1>&2; return 0; }
  printf ' %q' rm -f "$1" 1>&2; echo 1>&2
  rm -f "$1" || true
}

uninstall() {
  for DEST in "${DESTS[@]}"; do
    [[ ! -d "$DEST" ]] && continue
    for FILE in "${FILES[@]}"; do
      FILE="$(basename "$FILE")"
      rm_my_file "$DEST/$FILE"
      if file_alias "$(basename "$FILE")"; then
        rm_my_file "$DEST/$ALIAS"
      fi
    done
  done
}

