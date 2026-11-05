"""The Start menu entry: a Windows shortcut that opens the installed GUI.

It starts the installed copy's GUI with the windowless interpreter and names the store
it serves. It also carries MirrorHold's application identity (``APP_ID``): Windows
shows a notification only for an application whose Start menu entry declares its
identity, so this entry is what lets notices appear under MirrorHold's name. Windows
takes the icon for the entry, and for every notice under its identity, from the icon
the entry names; an entry naming none shows its target's own, the interpreter's.

Shortcuts are written through the Windows shell's own interfaces (``pywin32``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_ID = "MirrorHold.MirrorHold"
SHORTCUT_NAME = "MirrorHold.lnk"


def start_menu_dir() -> Path:
    """The current user's Start menu programs folder."""
    roaming = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(roaming) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def start_menu_shortcut() -> Path:
    return start_menu_dir() / SHORTCUT_NAME


@dataclass(frozen=True)
class Shortcut:
    target: str
    arguments: str
    working_dir: str
    description: str
    app_id: str = APP_ID
    icon: str = ""               # the icon file the entry shows; empty: its target's own


def _shell_link():
    import pythoncom
    from win32com.shell import shell
    return pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
                                      shell.IID_IShellLink)


def write_shortcut(path: Path, shortcut: Shortcut) -> None:
    """Write (or replace) the shortcut file at ``path``. Raises OSError."""
    try:
        import pythoncom
        from win32com.propsys import propsys, pscon
        link = _shell_link()
        link.SetPath(shortcut.target)
        link.SetArguments(shortcut.arguments)
        link.SetWorkingDirectory(shortcut.working_dir)
        link.SetDescription(shortcut.description)
        if shortcut.icon:
            link.SetIconLocation(shortcut.icon, 0)
        store = link.QueryInterface(propsys.IID_IPropertyStore)
        store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(shortcut.app_id, pythoncom.VT_LPWSTR))
        store.Commit()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(path), 0)
    except ImportError as exc:
        raise OSError(f"a shortcut needs Windows and pywin32: {exc}") from None
    except Exception as exc:  # pywintypes.com_error carries no OSError base
        if isinstance(exc, OSError):
            raise
        raise OSError(f"the shortcut could not be written: {exc}") from None


def read_shortcut(path: Path) -> Shortcut:
    """The shortcut file at ``path``. Raises OSError."""
    try:
        import pythoncom
        from win32com.propsys import propsys, pscon
        link = _shell_link()
        link.QueryInterface(pythoncom.IID_IPersistFile).Load(str(path))
        app_id = link.QueryInterface(propsys.IID_IPropertyStore).GetValue(pscon.PKEY_AppUserModel_ID).GetValue()
        icon, _ = link.GetIconLocation()
        return Shortcut(link.GetPath(0)[0], link.GetArguments(), link.GetWorkingDirectory(), link.GetDescription(),
                        app_id or "", icon or "")
    except ImportError as exc:
        raise OSError(f"a shortcut needs Windows and pywin32: {exc}") from None
    except Exception as exc:
        if isinstance(exc, OSError):
            raise
        raise OSError(f"the shortcut could not be read: {exc}") from None
