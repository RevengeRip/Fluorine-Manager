# Linux compatibility shim for Python plugins that import the Windows `winreg`
# module. Callers in our plugin set expect registry access to fail and fall back
# to non-registry paths.

HKEY_LOCAL_MACHINE = object()
HKEY_CURRENT_USER = object()


def OpenKey(*_args, **_kwargs):
    raise FileNotFoundError("winreg is not available on this platform")


OpenKeyEx = OpenKey


def QueryValueEx(*_args, **_kwargs):
    raise FileNotFoundError("winreg is not available on this platform")


def QueryInfoKey(*_args, **_kwargs):
    raise FileNotFoundError("winreg is not available on this platform")


def EnumKey(*_args, **_kwargs):
    raise FileNotFoundError("winreg is not available on this platform")
