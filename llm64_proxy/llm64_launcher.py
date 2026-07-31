#!/usr/bin/env python3
"""Entry point for the packaged proxy (PyInstaller builds this).

Double-clicking the binary opens the launcher window. `--headless`
runs the plain CLI server instead - same flags as `python -m src`
(--host, --port, --config, -v); on Windows a headless windowed exe has
no console, so watch <data_dir>/proxy.log.
"""

import multiprocessing
import sys


def run():
    multiprocessing.freeze_support()
    if '--headless' in sys.argv:
        sys.argv.remove('--headless')
        import asyncio
        from src.main import main
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass
    else:
        from src.launcher import main
        main()


if __name__ == '__main__':
    run()
