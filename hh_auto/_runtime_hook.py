import os
import sys


def get_hook_dirs():
    return []


def _patch_playwright_driver_path():
    try:
        import playwright._impl._driver as drv
    except ImportError:
        return

    if hasattr(drv, '_patched_for_pyinstaller'):
        return

    base = os.path.dirname(os.path.abspath(sys.executable))
    bundled_driver = os.path.join(base, '_internal', 'playwright', 'driver', 'node')
    if os.path.isfile(bundled_driver):
        _orig = getattr(drv, 'compute_driver_executable', None)
        if _orig:
            drv.compute_driver_executable = lambda: (
                bundled_driver,
                os.path.join(
                    os.path.dirname(bundled_driver), 'package', 'cli.js'
                ),
            )
            drv._patched_for_pyinstaller = True


_patch_playwright_driver_path()
