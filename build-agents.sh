#!/bin/bash

# Assemble recipes/browse-agents-<agent> from recipes/browse-agents-src/
# parts: common/[0-9]* plus <agent>/[0-9]*, merged by basename.

set -ueo pipefail

cd "$(realpath "$(git rev-parse --show-toplevel)")/recipes/browse-agents-src"
shopt -s nullglob

emit_part() {
    local f="$1"
    if [[ "${f%.*}" == *+ && -x "$f" ]]; then
        echo "# SOURCE START: $f {{{"
        ./"$f"
    else
        perl -npE '
            if ((!/^#!/ && !($x++)) .. $x++) {
                $ENV{N} eq "1" and say "\n# THIS IS A GENERATED FILE - DO NOT EDIT!";
                say "# SOURCE START: $ARGV {{{";
            }
        ' "$f"
    fi
    echo "# }}} # SOURCE END: $f"
    echo
}

for agent in */; do
    agent="${agent%/}"
    [[ "$agent" == common ]] && continue
    parts=("$agent"/[0-9]*)
    if (( ${#parts[@]} == 0 )); then
        echo "build-agents.sh: $agent/ has no parts, skipping" >&2
        continue
    fi
    out="../browse-agents-$agent"
    printf '%s\n' common/[0-9]* "${parts[@]}" | LC_ALL=C sort -s -t/ -k2,2 | (
        export N=1
        while read -r f; do
            emit_part "$f"
            export N=$((N+1))
        done
    ) > "$out.generated"
    mv -f "$out.generated" "$out"
    chmod 755 "$out"
done
