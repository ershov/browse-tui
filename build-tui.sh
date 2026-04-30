#!/bin/bash

set -ueo pipefail

cd "$(realpath "$(git rev-parse --show-toplevel)")"

(
    cd src-tui
    export N=1
    for f in [0-9]*; do
        if [[ "${f%.*}" == *+ && -x "$f" ]]; then
            echo "# SOURCE START: $f {{{"
            ./$f
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
        export N=$((N+1))
    done
) > browse-tui.generated

mv -f browse-tui.generated browse-tui

chmod 755 browse-tui
