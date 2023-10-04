import sys
import os

path = os.path.dirname(__file__)
if path not in sys.path:
    sys.path.append(path)

from .version import version

from .octoprint_3dprinteros import Plugin3DPrinterOS

__plugin_name__ = "3DPrinterOS"
__plugin_description__ = "Full remote control with 3DPrinterOS Cloud"
__plugin_pythoncompat__ = ">=3.7,<4"
__plugin_implementation__ = Plugin3DPrinterOS()
__plugin_version__ = version

def __plugin_load__():
    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
    }
