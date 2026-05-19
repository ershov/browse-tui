"""Recipe: long-yielding ``get_preview`` generator for #274 UI test.

Generator yields markers ``LINE_001\n``, ``LINE_002\n``, … indefinitely
(up to a sane bound). The buffer cap is set tight so the worker pauses
quickly; the UI test scrolls the preview pane and verifies new lines
appear after each demand-resume cycle.

Usage:  browse-tui --run-py preview_resume.py
"""

import sys

from browse_tui import Browser, BrowserConfig, Item


def main():
    def get_children(parent_id):
        if parent_id in (None, ''):
            return [Item(id='item', title='item')]
        return []

    def get_preview(item_id):
        # 200 lines, 12 chars each → ~2400 chars total. With cap of
        # 30 lines per pause window we see at least 5 pauses; the UI
        # test scrolls past the first cap and verifies later lines
        # appear after the demand-resume.
        for i in range(1, 201):
            yield f'LINE_{i:03d}\n'

    b = Browser(BrowserConfig(
        get_children=get_children,
        get_preview=get_preview,
        show_ids='always',
        # Tight cap so the first pause happens with plenty of buffer
        # below to scroll past — UI test wants room for the demand
        # threshold to clearly trigger.
        preview_buffer_cap_chars=1_000_000,
        preview_buffer_cap_lines=30,
    ))
    sys.exit(b.run())


if __name__ == '__main__':
    main()
