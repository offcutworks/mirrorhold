"""Handing a toast to Windows through the Windows Runtime, from this process.

The core process that raises a notice calls Windows itself: the runtime's XML document
takes the toast's content, the toast notification class wraps it, and the notifier of an
application identity shows it. No other program is started for it — a hidden shell given
an encoded script is what behaviour-based antivirus watches for — and nothing beyond the
standard library is needed: the calls go through ``combase`` with ``ctypes``.

Each runtime interface is a COM vtable. Its identifier and the order of its methods are
those Windows' own metadata declares (``C:\\Windows\\System32\\WinMetadata``, read through
the runtime's projection on the development machine): after IUnknown's three slots and
IInspectable's three, an interface's first method is slot 6. A wrong slot calls another
method with these arguments, so each one used is named here beside its interface.

The work runs on a thread of its own, which enters the runtime's multithreaded apartment
and leaves it once the toast is handed over: the calling thread may already be in an
apartment of another kind, which the runtime would refuse to enter.
"""
from __future__ import annotations

import ctypes
import sys
import threading
import uuid
from typing import List, Optional, Sequence

XML_DOCUMENT = "Windows.Data.Xml.Dom.XmlDocument"
TOAST_NOTIFICATION = "Windows.UI.Notifications.ToastNotification"
TOAST_NOTIFICATION_MANAGER = "Windows.UI.Notifications.ToastNotificationManager"

IID_XML_DOCUMENT = "F7F3A506-1E87-42D6-BCFB-B8C809FA5494"
IID_XML_DOCUMENT_IO = "6CD0E74E-EE65-4489-9EBF-CA43E87BA637"
IID_TOAST_NOTIFICATION_FACTORY = "04124B20-82C6-4229-B109-FD9ED4662B53"
IID_TOAST_NOTIFICATION_MANAGER_STATICS = "50AC103F-D235-4598-BBEF-98FE4D1A3AD4"

QUERY_INTERFACE = 0               # IUnknown
RELEASE = 2                       # IUnknown
LOAD_XML = 6                      # IXmlDocumentIO.LoadXml(HSTRING)
CREATE_TOAST_NOTIFICATION = 6     # IToastNotificationFactory.CreateToastNotification(IXmlDocument*, out)
CREATE_TOAST_NOTIFIER_WITH_ID = 7  # IToastNotificationManagerStatics.CreateToastNotifier(HSTRING, out IToastNotifier*)
SHOW = 6                          # IToastNotifier {75927B93-03F3-41EC-91D3-6E5BAC1B38E7}.Show(IToastNotification*)

RO_INIT_MULTITHREADED = 1
TIMEOUT = 30.0


class ToastRefused(RuntimeError):
    """Windows did not take the toast; the message says at which step, and why."""


def show(xml: str, app_id: str, *, timeout: float = TIMEOUT) -> None:
    """Show the toast ``xml`` (the toast schema) under the application identity ``app_id``.
    Raises ToastRefused."""
    _on_a_thread_of_its_own(xml, app_id, True, timeout)


def check(xml: str, app_id: str, *, timeout: float = TIMEOUT) -> None:
    """Everything ``show`` does but the showing: the content read, the toast made and the
    identity's notifier obtained, with nothing reaching the screen. Raises ToastRefused."""
    _on_a_thread_of_its_own(xml, app_id, False, timeout)


def _on_a_thread_of_its_own(xml: str, app_id: str, deliver: bool, timeout: float) -> None:
    """Hand the toast over on a worker thread, waiting ``timeout`` seconds. A worker still
    busy then is told to give up before it shows the toast, so a toast reported as not
    shown appears afterwards only if Windows was already showing it."""
    if sys.platform != "win32":
        raise ToastRefused("Windows notifications need Windows")
    outcome: List[Optional[ToastRefused]] = []
    abandoned = threading.Event()

    def work():
        try:
            _hand_over(xml, app_id, deliver, abandoned)
        except ToastRefused as exc:
            outcome.append(exc)
        except (OSError, AttributeError, ctypes.ArgumentError) as exc:   # combase missing or unusable
            outcome.append(ToastRefused(f"the Windows Runtime is not available: {exc}"))
        except Exception as exc:   # the caller is told what happened, never a timeout instead
            outcome.append(ToastRefused(f"the toast could not be handed over: {exc!r}"))
        else:
            outcome.append(None)

    worker = threading.Thread(target=work, name="mirrorhold-toast", daemon=True)
    worker.start()
    worker.join(timeout)
    if not outcome:
        abandoned.set()
        raise ToastRefused(f"Windows did not take the toast within {timeout:.0f} s")
    if outcome[0] is not None:
        raise outcome[0]


def _guid(text: str):
    return (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(text).bytes_le)


class _Runtime:
    """The ``combase`` calls a toast needs, made inside one apartment, with every string and
    interface it obtained released when it is closed."""

    def __init__(self):
        combase = ctypes.WinDLL("combase")
        self._initialize = combase.RoInitialize
        self._initialize.argtypes = (ctypes.c_int,)
        self._initialize.restype = ctypes.HRESULT
        self._uninitialize = combase.RoUninitialize
        self._uninitialize.argtypes = ()
        self._uninitialize.restype = None
        self._create_string = combase.WindowsCreateString
        self._create_string.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p))
        self._create_string.restype = ctypes.HRESULT
        self._delete_string = combase.WindowsDeleteString
        self._delete_string.argtypes = (ctypes.c_void_p,)
        self._delete_string.restype = ctypes.HRESULT
        self._activate = combase.RoActivateInstance
        self._activate.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        self._activate.restype = ctypes.HRESULT
        self._factory = combase.RoGetActivationFactory
        self._factory.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        self._factory.restype = ctypes.HRESULT
        self._strings: List[ctypes.c_void_p] = []
        self._interfaces: List[ctypes.c_void_p] = []
        self._entered = False

    def enter(self) -> None:
        self._step("entering the Windows Runtime", self._initialize, RO_INIT_MULTITHREADED)
        self._entered = True

    def close(self) -> None:
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        for pointer in reversed(self._interfaces):
            release(_slot(pointer, RELEASE))(pointer)
        for string in self._strings:
            self._delete_string(string)
        self._interfaces, self._strings = [], []
        if self._entered:
            self._uninitialize()
            self._entered = False

    @staticmethod
    def _step(what: str, function, *args):
        try:
            return function(*args)
        except OSError as exc:
            code = (getattr(exc, "winerror", None) or 0) & 0xFFFFFFFF
            reason = (exc.strerror or "").strip()
            if not reason or reason.lower().startswith("windows error"):   # Windows has no words for it
                raise ToastRefused(f"{what}: error 0x{code:08X}") from None
            raise ToastRefused(f"{what}: {reason} (0x{code:08X})") from None

    def string(self, text: str) -> ctypes.c_void_p:
        """An HSTRING of ``text``; its length counts UTF-16 units, so a character outside the
        basic plane counts twice."""
        units = len(text.encode("utf-16-le", "surrogatepass")) // 2
        handle = ctypes.c_void_p()
        self._step("making a string", self._create_string, text, units, ctypes.byref(handle))
        self._strings.append(handle)
        return handle

    def _held(self, pointer: ctypes.c_void_p, what: str) -> ctypes.c_void_p:
        if not pointer.value:
            raise ToastRefused(f"{what}: Windows returned nothing")
        self._interfaces.append(pointer)
        return pointer

    def activate(self, class_name: str) -> ctypes.c_void_p:
        instance = ctypes.c_void_p()
        self._step(f"making a {class_name}", self._activate, self.string(class_name), ctypes.byref(instance))
        return self._held(instance, f"making a {class_name}")

    def factory(self, class_name: str, iid: str) -> ctypes.c_void_p:
        found = ctypes.c_void_p()
        self._step(f"reaching {class_name}", self._factory, self.string(class_name), ctypes.byref(_guid(iid)),
                   ctypes.byref(found))
        return self._held(found, f"reaching {class_name}")

    def query(self, pointer: ctypes.c_void_p, iid: str, what: str) -> ctypes.c_void_p:
        found = ctypes.c_void_p()
        self.call(pointer, QUERY_INTERFACE, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)), what,
                  ctypes.byref(_guid(iid)), ctypes.byref(found))
        return self._held(found, what)

    def call(self, pointer: ctypes.c_void_p, slot: int, argtypes: Sequence, what: str, *args) -> None:
        method = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(_slot(pointer, slot))
        self._step(what, method, pointer, *args)

    def made(self, pointer: ctypes.c_void_p, slot: int, argtypes: Sequence, what: str, *args) -> ctypes.c_void_p:
        """What the method at ``slot`` returns through its last, out parameter."""
        result = ctypes.c_void_p()
        self.call(pointer, slot, (*argtypes, ctypes.POINTER(ctypes.c_void_p)), what, *args, ctypes.byref(result))
        return self._held(result, what)


def _slot(pointer: ctypes.c_void_p, slot: int) -> int:
    """The address of the method at ``slot`` of the interface ``pointer``."""
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return vtable[slot]


def _hand_over(xml: str, app_id: str, deliver: bool, abandoned: threading.Event) -> None:
    runtime = _Runtime()
    try:
        runtime.enter()
        document = runtime.activate(XML_DOCUMENT)
        reader = runtime.query(document, IID_XML_DOCUMENT_IO, "reading the toast's content")
        runtime.call(reader, LOAD_XML, (ctypes.c_void_p,), "reading the toast's content", runtime.string(xml))
        content = runtime.query(document, IID_XML_DOCUMENT, "reading the toast's content")
        toasts = runtime.factory(TOAST_NOTIFICATION, IID_TOAST_NOTIFICATION_FACTORY)
        toast = runtime.made(toasts, CREATE_TOAST_NOTIFICATION, (ctypes.c_void_p,), "making the toast", content)
        manager = runtime.factory(TOAST_NOTIFICATION_MANAGER, IID_TOAST_NOTIFICATION_MANAGER_STATICS)
        notifier = runtime.made(manager, CREATE_TOAST_NOTIFIER_WITH_ID, (ctypes.c_void_p,),
                                f"reaching the notifier of {app_id}", runtime.string(app_id))
        if deliver and not abandoned.is_set():
            runtime.call(notifier, SHOW, (ctypes.c_void_p,), "showing the toast", toast)
    finally:
        runtime.close()
