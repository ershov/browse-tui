"""Recipe: list lines of a file, refresh on file change via watcher.

Usage:
    browse-tui --run-py fs_watcher.py PATH

The browser shows one Item per non-blank line of ``PATH``; a watcher
thread polls the file every 0.2s and triggers a refresh whenever the
contents change. Used by the background-update UI test.
"""

import sys
import time

from browse_tui import Browser, BrowserConfig, Item


def main():
    path = sys.argv[1]

    def get_children(_parent_id, *, reload=False):
        try:
            with open(path) as f:
                return [Item(id=line.strip(), title=line.strip())
                        for line in f if line.strip()]
        except OSError:
            return []

    # show_ids='always' keeps the rendered shape stable for UI tests
    # even though the recipe sets id == title.
    b = Browser(BrowserConfig(get_children=get_children, show_ids='always'))

    def watcher(browser):
        last = None
        while True:
            time.sleep(0.2)
            try:
                with open(path) as f:
                    cur = f.read()
            except OSError:
                cur = ''
            if cur != last:
                browser.refresh()
                last = cur

    b.watch(watcher)
    sys.exit(b.run())


if __name__ == '__main__':
    main()
