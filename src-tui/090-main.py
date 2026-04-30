"""browse-tui: main loop and entry point."""


def main(argv):
    if len(argv) >= 2 and argv[1] == '--version':
        print(__version__)
        return 0
    # full TUI dispatch lands in later tickets
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
