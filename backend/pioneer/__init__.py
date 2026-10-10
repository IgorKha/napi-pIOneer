import os as _os
import sys as _sys

__version__ = "0.0.1"

# Чисто-питоновские зависимости, которых нет на плате (paho-mqtt), лежат в pioneer/vendor (spec 9)
_VENDOR = _os.path.join(_os.path.dirname(__file__), "vendor")
if _VENDOR not in _sys.path:
    _sys.path.append(_VENDOR)
