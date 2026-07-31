"""Where the files that ship inside the package actually are.

Running from a checkout, data files (default_cards/, the rules and
overrides JSON) sit next to the modules and __file__ finds them. In a
PyInstaller binary they don't: modules live in a bundled archive and
the data files are extracted to sys._MEIPASS (onefile) or _internal/
(onedir), with the spec's datas placing them under 'src/' to mirror
the checkout. Resolve through here instead of __file__ so both layouts
work.
"""

import sys
from pathlib import Path


def resource_dir() -> Path:
    """The directory bundled data files resolve against."""
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', None)
        if base is None:
            base = Path(sys.executable).resolve().parent
        return Path(base) / 'src'
    return Path(__file__).resolve().parent
