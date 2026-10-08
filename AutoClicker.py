# Теперь у вас есть выбор между ручным вводом интервалов и
# случайным выбором интервалов с помощью радиокнопок. При ручном
# вводе интервалов, на любом этапе можно нажать кнопку "Set same for all following",
# чтобы задать одинаковый интервал для всех последующих повторов.
#Эти изменения позволят пользователю видеть, что программа запущена на воспроизведение, и сколько времени осталось до следующего выполнения записанных действий.
import tkinter as tk
from tkinter import messagebox, filedialog, simpledialog, ttk
from pynput import mouse, keyboard
from pynput.mouse import Button
import ctypes
from ctypes import wintypes
import threading
import time
import json
import os
import random

try:
    import qrcode  # нужна только для QR-кода в окне доната; без неё программа работает, просто без QR
except ImportError:
    qrcode = None


def enable_dpi_awareness():
    # pynput при записи получает физические пиксели, а SetCursorPos в процессе без DPI-awareness
    # трактует координаты как логические. При масштабе экрана > 100% воспроизведение уезжало со
    # сдвигом (125% -> x1.25). DPI-awareness нужно включить до создания окна Tk.
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


enable_dpi_awareness()


def use_dark_title_bar(window):
    # Без этого заголовок окна остаётся белым и выбивается из тёмной темы (Windows 10 1809+ / 11)
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        window.update_idletasks()
        hwnd = ctypes.c_void_p(user32.GetParent(window.winfo_id()))
        enabled = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(enabled), ctypes.sizeof(enabled))
        # Windows сама не перерисовывает уже показанный заголовок: переключаем активность рамки
        # (WM_NCACTIVATE), не меняя того, активно ли окно на самом деле
        was_active = user32.GetForegroundWindow() == hwnd.value
        user32.SendMessageW(hwnd, 0x0086, 0, 0)
        if was_active:
            user32.SendMessageW(hwnd, 0x0086, 1, 0)
    except (AttributeError, OSError):
        pass


def set_clipboard_text(window, text):
    # Буфер обмена Tk на Windows отдаёт данные «по запросу» и теряет их, когда программа закрывается:
    # скопировал адрес, закрыл окно, а вставить нечего. Кладём текст в системный буфер сразу.
    # Владельцем делаем внешнюю рамку окна: если владелец - внутреннее окно Tk (winfo_id), при закрытии
    # программы Tk очищает буфер, а с рамкой текст остаётся.
    if os.name != "nt":
        return False
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetParent.restype = ctypes.c_void_p
    user32.GetParent.argtypes = [ctypes.c_void_p]
    hwnd = user32.GetParent(window.winfo_id())
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    data = text.encode("utf-16-le") + b"\0\0"
    handle = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
    if not handle:
        return False
    ctypes.memmove(kernel32.GlobalLock(handle), data, len(data))
    kernel32.GlobalUnlock(handle)
    for _attempt in range(10):  # буфер на миг может быть занят другой программой
        if user32.OpenClipboard(hwnd):
            break
        time.sleep(0.02)
    else:
        kernel32.GlobalFree(handle)
        return False
    try:
        user32.EmptyClipboard()
        if user32.SetClipboardData(13, handle):  # CF_UNICODETEXT; дальше память принадлежит системе
            return True
        kernel32.GlobalFree(handle)
        return False
    finally:
        user32.CloseClipboard()


# Горячие клавиши: (последовательность Tk, как показать, ключ описания в TRANSLATIONS).
# Из этой таблицы строятся и привязки клавиш, и окно подсказки.
HOTKEYS = [
    ("<Control-t>", "Ctrl+T", "hk_start_recording"),
    ("<Control-r>", "Ctrl+R", "hk_stop_recording"),
    ("<Control-e>", "Ctrl+E", "hk_start_playback"),
    ("<Control-w>", "Ctrl+W", "hk_stop_playback"),
    ("<F1>", "F1", "hk_help"),
]

# Донаты: один адрес (Rabby Wallet) на все EVM-сети
DONATION_ADDRESS = "0x174e74791362b91bfab12445f279ce6e062e3c79"
DONATION_NETWORKS = ["Ethereum", "BNB Chain", "Arbitrum", "Polygon", "Optimism", "Mantle"]


def donation_qr_matrix():
    # QR строится из того же DONATION_ADDRESS, что показан текстом, чтобы они не могли разойтись.
    # В QR - голый адрес: так его читают все EVM-кошельки (ссылка ethereum: привязала бы к одной сети).
    if qrcode is None:
        return None
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    qr.add_data(DONATION_ADDRESS)
    qr.make(fit=True)
    return qr.get_matrix()

TRANSLATIONS = {
    "en": {
        "app_title": "Mouse and Keyboard Recorder",
        "card_recording": "Recording",
        "card_playback": "Playback",
        "start_recording": "Start Recording",
        "stop_recording": "Stop Recording",
        "start_playback": "Start Playback",
        "stop_playback": "Stop Playback",
        "save_recording": "Save Recording",
        "load_recording": "Load Recording",
        "game_mode": "Game mode (raw input)",
        "hotkeys_button": "?  Hotkeys",
        "donate_button": "♥  Donate",
        "donate_title": "Support the author",
        "donate_intro": "If you find this program useful, you can support the author with crypto.",
        "donate_networks": "Networks",
        "donate_networks_more": "and other EVM networks",
        "donate_address": "Address",
        "donate_copy": "Copy address",
        "donate_copied": "Copied ✓",
        "donate_warning": "One address works for all EVM networks. Check the network in your wallet before "
                          "sending. Do not send from non-EVM networks (Bitcoin, Solana, Tron): the funds "
                          "would be lost.",
        "status_idle": "Idle",
        "status_recording": "Recording",
        "status_playing": "Playing",
        "status_stopping": "Stopping...",
        "no_recordings": "No recordings loaded",
        "next_repeat": "Next repeat in: {remaining}s (Repeat {current}/{total})",
        "executing_repeat": "Executing repeat {current}/{total}",
        "hotkeys_title": "Keyboard shortcuts",
        "hotkeys_note": "Shortcuts work while this program's window is active.",
        "close": "Close",
        "hk_start_recording": "Start recording",
        "hk_stop_recording": "Stop recording",
        "hk_start_playback": "Start playback",
        "hk_stop_playback": "Stop playback",
        "hk_help": "Show this help",
        "interval_method_title": "Choose Interval Method",
        "manual": "Manual",
        "random": "Random",
        "ok": "OK",
        "cancel": "Cancel",
        "repeat_count_title": "Repeat Count",
        "repeat_count_prompt": "Enter the number of repeats:",
        "interval_title": "Enter Interval {n}",
        "interval_prompt": "Enter interval (s) before repeat {n}:",
        "set_same_for_all": "Set same for all following",
        "invalid_title": "Invalid input",
        "invalid_number": "Please enter a valid number.",
        "min_interval_title": "Minimum Interval",
        "min_interval_prompt": "Enter the minimum interval (s):",
        "max_interval_title": "Maximum Interval",
        "max_interval_prompt": "Enter the maximum interval (s):",
        "json_files": "JSON files",
    },
    "ru": {
        "app_title": "Запись мыши и клавиатуры",
        "card_recording": "Запись",
        "card_playback": "Воспроизведение",
        "start_recording": "Начать запись",
        "stop_recording": "Остановить запись",
        "start_playback": "Начать воспроизведение",
        "stop_playback": "Остановить воспроизведение",
        "save_recording": "Сохранить запись",
        "load_recording": "Загрузить запись",
        "game_mode": "Режим для игр (сырой ввод)",
        "hotkeys_button": "?  Горячие клавиши",
        "donate_button": "♥  Поддержать",
        "donate_title": "Поддержать автора",
        "donate_intro": "Если программа вам полезна, можно поддержать автора криптовалютой.",
        "donate_networks": "Сети",
        "donate_networks_more": "и другие EVM-сети",
        "donate_address": "Адрес",
        "donate_copy": "Копировать адрес",
        "donate_copied": "Скопировано ✓",
        "donate_warning": "Один адрес для всех EVM-сетей. Перед отправкой проверьте сеть в своём кошельке. "
                          "В сетях не из EVM (Bitcoin, Solana, Tron) этот адрес использовать нельзя: "
                          "средства будут потеряны.",
        "status_idle": "Ожидание",
        "status_recording": "Идёт запись",
        "status_playing": "Идёт воспроизведение",
        "status_stopping": "Остановка...",
        "no_recordings": "Записи не загружены",
        "next_repeat": "Следующий повтор через {remaining} с (повтор {current}/{total})",
        "executing_repeat": "Выполняется повтор {current}/{total}",
        "hotkeys_title": "Горячие клавиши",
        "hotkeys_note": "Горячие клавиши работают, пока окно программы активно.",
        "close": "Закрыть",
        "hk_start_recording": "Начать запись",
        "hk_stop_recording": "Остановить запись",
        "hk_start_playback": "Начать воспроизведение",
        "hk_stop_playback": "Остановить воспроизведение",
        "hk_help": "Показать эту подсказку",
        "interval_method_title": "Способ задания интервалов",
        "manual": "Вручную",
        "random": "Случайно",
        "ok": "OK",
        "cancel": "Отмена",
        "repeat_count_title": "Число повторов",
        "repeat_count_prompt": "Введите число повторов:",
        "interval_title": "Интервал {n}",
        "interval_prompt": "Интервал (с) перед повтором {n}:",
        "set_same_for_all": "Одинаковый для всех следующих",
        "invalid_title": "Неверный ввод",
        "invalid_number": "Введите корректное число.",
        "min_interval_title": "Минимальный интервал",
        "min_interval_prompt": "Введите минимальный интервал (с):",
        "max_interval_title": "Максимальный интервал",
        "max_interval_prompt": "Введите максимальный интервал (с):",
        "json_files": "Файлы JSON",
    },
}

COLORS = {
    "bg": "#1b1d25", "card": "#262933", "surface": "#333747", "surface_hover": "#3f4458",
    "text": "#e8eaf0", "muted": "#8d93a5", "border": "#3a3e4d",
    "disabled_bg": "#2d303c", "disabled_fg": "#5f6579",
    "red": "#e5484d", "red_hover": "#f0666b", "green": "#3ecf8e", "green_hover": "#5bdba0",
    "amber": "#f5a524", "blue": "#5b8def", "idle": "#6b7186",
}
STATUS_COLORS = {
    "idle": COLORS["idle"], "recording": COLORS["red"],
    "playing": COLORS["green"], "stopping": COLORS["amber"],
}
FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_SMALL_BOLD = ("Segoe UI", 8, "bold")
FONT_TITLE = ("Segoe UI", 14, "bold")
FONT_KEY = ("Consolas", 10, "bold")
FONT_MONO = ("Consolas", 10)


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class _Input(ctypes.Structure):
    class _Union(ctypes.Union):
        _fields_ = [("mi", _MouseInput)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _Union)]


class _RawInputDevice(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                ("dwFlags", wintypes.DWORD), ("hwndTarget", ctypes.c_void_p)]


class _RawInputHeader(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                ("hDevice", ctypes.c_void_p), ("wParam", ctypes.c_size_t)]


class _RawMouse(ctypes.Structure):
    _fields_ = [("usFlags", wintypes.USHORT), ("ulButtons", wintypes.ULONG), ("ulRawButtons", wintypes.ULONG),
                ("lLastX", wintypes.LONG), ("lLastY", wintypes.LONG), ("ulExtraInformation", wintypes.ULONG)]


class _RawInput(ctypes.Structure):
    _fields_ = [("header", _RawInputHeader), ("mouse", _RawMouse)]


def send_relative_move(dx, dy):
    # Движение «как от железа»: в отличие от SetCursorPos оно порождает сырой ввод мыши,
    # который читают игры (например, вращение камеры правой кнопкой в Roblox)
    event = _Input(type=0, mi=_MouseInput(dx, dy, 0, 0x0001, 0, None))  # INPUT_MOUSE, MOUSEEVENTF_MOVE
    ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event))


class RawMouseListener:
    """Слушает сырые относительные смещения мыши (Windows Raw Input) в отдельном потоке.

    pynput отдаёт только координаты курсора, а игры и 3D-программы читают именно эти смещения.
    """
    WM_INPUT = 0x00FF
    WM_QUIT = 0x0012

    def __init__(self, on_move):
        self.on_move = on_move
        self.thread = None
        self.thread_id = None
        self.ready = threading.Event()
        self.available = False
        self.user32 = None

    def start(self):
        if os.name != "nt":
            return
        self.user32 = self._load_user32()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready.wait(2)

    def stop(self):
        if self.thread is None:
            return
        if self.thread_id:
            self.user32.PostThreadMessageW(self.thread_id, self.WM_QUIT, 0, 0)
        self.thread.join(timeout=2)
        self.thread = None

    @staticmethod
    def _load_user32():
        # Своя копия user32: не трогаем сигнатуры функций, которые используются в остальной программе
        user32 = ctypes.WinDLL("user32")
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        user32.DestroyWindow.argtypes = [ctypes.c_void_p]
        user32.RegisterRawInputDevices.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
        user32.GetRawInputData.restype = wintypes.UINT
        user32.GetRawInputData.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p,
                                           ctypes.POINTER(wintypes.UINT), wintypes.UINT]
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        return user32

    def _run(self):
        user32 = self.user32
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        # Окно-невидимка (message-only): ему адресуются сообщения WM_INPUT
        hwnd = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, ctypes.c_void_p(-3), None, None, None)
        if hwnd:
            device = _RawInputDevice(1, 2, 0x100, hwnd)  # мышь, RIDEV_INPUTSINK: слушать и в фоне
            self.available = bool(user32.RegisterRawInputDevices(ctypes.byref(device), 1, ctypes.sizeof(device)))
        self.ready.set()
        if not self.available:
            if hwnd:
                user32.DestroyWindow(hwnd)
            return
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message == self.WM_INPUT:
                try:
                    self._handle(message.lParam)
                except Exception:  # сбой обработки одного события не должен убить запись остальных
                    pass
        user32.DestroyWindow(hwnd)

    def _handle(self, lparam):
        raw = _RawInput()
        size = wintypes.UINT(ctypes.sizeof(raw))
        copied = self.user32.GetRawInputData(ctypes.c_void_p(lparam), 0x10000003, ctypes.byref(raw),
                                             ctypes.byref(size), ctypes.sizeof(_RawInputHeader))
        if copied == 0xFFFFFFFF or raw.header.dwType != 0:
            return
        if raw.mouse.usFlags & 1:  # абсолютные координаты (планшет, удалённый стол): нужны только относительные
            return
        if raw.mouse.lLastX or raw.mouse.lLastY:
            self.on_move(raw.mouse.lLastX, raw.mouse.lLastY)


class Recorder:
    def __init__(self):
        self.recording = False
        self.playing = False
        self.events = []
        self.start_time = None
        self.playback_thread = None
        self.stop_event = threading.Event()
        self.update_gui_callback = None
        self.raw_listener = None
        self.game_mode = False

    def start_recording(self):
        self.recording = True
        self.start_time = time.time()
        self.events = []
        self.mouse_listener = mouse.Listener(on_click=self.on_click, on_move=self.on_move,
                                             on_scroll=self.on_scroll)
        self.keyboard_listener = keyboard.Listener(on_press=self.on_press, on_release=self.on_release)
        self.raw_listener = RawMouseListener(self.on_raw_move)
        self.mouse_listener.start()
        self.keyboard_listener.start()
        self.raw_listener.start()

    def stop_recording(self, ignore_click_areas=()):
        self.recording = False
        self.mouse_listener.stop()
        self.keyboard_listener.stop()
        if self.raw_listener:
            self.raw_listener.stop()
        # События приходят из разных потоков (хуки pynput и сырой ввод): выстраиваем по времени
        self.events.sort(key=lambda e: e['time'])
        # Клики по кнопкам самой программы (Stop Recording) не должны попасть в запись
        self.events = [e for e in self.events if not self._is_click_in_areas(e, ignore_click_areas)]

    @staticmethod
    def _is_click_in_areas(event, areas):
        return event['type'] == 'click' and any(
            left <= event['x'] < right and top <= event['y'] < bottom for left, top, right, bottom in areas)

    def start_playback(self, intervals):
        self.playing = True
        self.stop_event.clear()
        self.playback_thread = threading.Thread(target=self.playback, args=(intervals,))
        self.playback_thread.start()

    def stop_playback(self):
        self.stop_event.set()
        self.playing = False

    def playback(self, intervals):
        game_mode = self.game_mode
        for i, interval in enumerate(intervals):
            if self.stop_event.is_set():
                break
            start_time = time.time()
            right_held = False
            for event in self.events:
                if self.stop_event.is_set():
                    break
                while time.time() - start_time < event['time']:
                    if self.stop_event.is_set():
                        break
                    time.sleep(0.01)
                if self.stop_event.is_set():
                    break
                if event['type'] == 'move':
                    # В режиме для игр при зажатой правой кнопке движение идёт сырыми смещениями (raw_move)
                    if not (game_mode and right_held):
                        mouse.Controller().position = (event['x'], event['y'])
                elif event['type'] == 'raw_move':
                    if game_mode and right_held:
                        send_relative_move(event['dx'], event['dy'])
                elif event['type'] == 'click':
                    button = getattr(Button, event['button'])
                    if event['button'] == 'right':
                        right_held = event['action'] == 'press'
                    if event['action'] == 'press':
                        mouse.Controller().press(button)
                    else:
                        if game_mode and event['button'] == 'right':
                            # Относительные смещения копят погрешность: возвращаем курсор в записанную точку
                            mouse.Controller().position = (event['x'], event['y'])
                        mouse.Controller().release(button)
                elif event['type'] == 'scroll':
                    mouse.Controller().scroll(event['dx'], event['dy'])
                elif event['type'] == 'key':
                    key = self._create_key_from_string(event['key'])
                    if event['action'] == 'press':
                        keyboard.Controller().press(key)
                    else:
                        keyboard.Controller().release(key)

            if interval > 0 and not self.stop_event.is_set():
                for remaining in range(int(interval), 0, -1):
                    if self.stop_event.is_set():
                        break
                    if self.update_gui_callback:
                        self.update_gui_callback(remaining, i + 1, len(intervals))
                    time.sleep(1)

        self.playing = False
        if self.update_gui_callback:
            self.update_gui_callback(0, 0, 0)

    def on_move(self, x, y):
        if self.recording:
            self.events.append({
                'type': 'move',
                'time': time.time() - self.start_time,
                'x': x,
                'y': y
            })

    def on_click(self, x, y, button, pressed):
        if self.recording:
            self.events.append({
                'type': 'click',
                'time': time.time() - self.start_time,
                'x': x,
                'y': y,
                'button': button.name,
                'action': 'press' if pressed else 'release'
            })

    def on_raw_move(self, dx, dy):
        if self.recording:
            self.events.append({
                'type': 'raw_move',
                'time': time.time() - self.start_time,
                'dx': dx,
                'dy': dy
            })

    def on_scroll(self, x, y, dx, dy):
        if self.recording:
            self.events.append({
                'type': 'scroll',
                'time': time.time() - self.start_time,
                'x': x,
                'y': y,
                'dx': dx,
                'dy': dy
            })

    def on_press(self, key):
        if self.recording:
            self.events.append({
                'type': 'key',
                'time': time.time() - self.start_time,
                'key': self._key_to_string(key),
                'action': 'press'
            })

    def on_release(self, key):
        if self.recording:
            self.events.append({
                'type': 'key',
                'time': time.time() - self.start_time,
                'key': self._key_to_string(key),
                'action': 'release'
            })

    def _key_to_string(self, key):
        if isinstance(key, keyboard.Key):
            return key.name
        else:
            return key.char

    def _create_key_from_string(self, key_string):
        try:
            return keyboard.Key[key_string]
        except KeyError:
            return keyboard.KeyCode.from_char(key_string)


class App:
    def __init__(self, root):
        self.root = root
        self.language = "ru"
        self.status_kind = "idle"
        self.hotkeys_window = None
        self.donate_window = None
        self.donate_copy_button = None
        self.donate_qr = None
        self._donate_copied = False  # показывать «Скопировано ✓» вместо «Копировать адрес»
        self._donate_reset_timer = None
        self._texts = []  # (виджет, ключ перевода, функция-преобразование) для смены языка на лету
        self._countdown = None  # ('wait', осталось, повтор, всего) | ('exec', повтор, всего) | None
        self.recorder = Recorder()

        self.dpi_scale = root.winfo_fpixels("1i") / 96
        self.setup_theme()
        self.root.resizable(False, False)

        main = ttk.Frame(root, padding=self.px(16))
        main.pack(fill=tk.BOTH, expand=True)
        # Невидимая распорка: держит ширину окна одинаковой в обоих языках
        tk.Frame(main, width=self.px(380), height=0, bg=COLORS["bg"]).pack()

        self.build_header(main)
        self.build_status(main)

        recording_card = self.make_card(main, "card_recording")
        self.record_button = self.make_button(recording_card, "start_recording", self.start_recording,
                                              style="Record.TButton")
        self.stop_recording_button = self.make_button(recording_card, "stop_recording", self.stop_recording,
                                                      state=tk.DISABLED)
        file_row = ttk.Frame(recording_card, style="Card.TFrame")
        file_row.pack(fill=tk.X, pady=(self.px(3), 0))
        file_row.columnconfigure((0, 1), weight=1, uniform="files")
        self.save_button = self.make_button(file_row, "save_recording", self.save_events, state=tk.DISABLED,
                                            grid=(0, 0, (0, self.px(3))))
        self.load_button = self.make_button(file_row, "load_recording", self.load_events,
                                            grid=(0, 1, (self.px(3), 0)))

        playback_card = self.make_card(main, "card_playback")
        self.play_button = self.make_button(playback_card, "start_playback", self.choose_interval_method,
                                            style="Play.TButton", state=tk.DISABLED)
        self.stop_playback_button = self.make_button(playback_card, "stop_playback", self.stop_playback,
                                                     state=tk.DISABLED)

        self.game_mode_var = tk.BooleanVar(value=False)
        self.game_mode_check = ttk.Checkbutton(playback_card, variable=self.game_mode_var,
                                               style="Card.TCheckbutton")
        self.bind_text(self.game_mode_check, "game_mode")
        self.game_mode_check.pack(anchor=tk.W, pady=(self.px(8), 0))

        self.recordings = []
        self.recording_var = tk.StringVar(root)
        self.recording_menu = ttk.Combobox(playback_card, textvariable=self.recording_var, state="readonly",
                                           font=FONT)
        self.recording_menu.pack(fill=tk.X, pady=(self.px(8), 0))
        self.update_recordings_menu()

        self.countdown_label = ttk.Label(playback_card, text="", style="CardMuted.TLabel",
                                         wraplength=self.px(340))
        self.countdown_label.pack(fill=tk.X, pady=(self.px(8), 0))

        footer = ttk.Frame(main)
        footer.pack(fill=tk.X)
        self.donate_button = ttk.Button(footer, style="Ghost.TButton", command=self.show_donate)
        self.bind_text(self.donate_button, "donate_button")
        self.donate_button.pack(side=tk.LEFT)
        self.hotkeys_button = ttk.Button(footer, style="Ghost.TButton", command=self.show_hotkeys)
        self.bind_text(self.hotkeys_button, "hotkeys_button")
        self.hotkeys_button.pack(side=tk.RIGHT)

        actions = {
            "hk_start_recording": self.start_recording,
            "hk_stop_recording": self.stop_recording,
            "hk_start_playback": self.choose_interval_method,
            "hk_stop_playback": self.stop_playback,
            "hk_help": self.show_hotkeys,
        }
        for sequence, _display, desc_key in HOTKEYS:
            self.root.bind(sequence, lambda e, action=actions[desc_key]: action())

        self.recorder.update_gui_callback = self.update_gui
        self.set_language(self.language)
        # Рамку окна красим, когда окно уже показано: до этого Windows перерисует её обратно в светлую
        self.root.bind("<Map>", lambda e: use_dark_title_bar(self.root) if e.widget is self.root else None)

    # ------------------------------------------------------------ оформление

    def px(self, value):
        return round(value * self.dpi_scale)

    def setup_theme(self):
        c = COLORS
        px = self.px
        self.root.configure(bg=c["bg"])
        for option, value in (("background", c["surface"]), ("foreground", c["text"]),
                              ("selectBackground", c["blue"]), ("selectForeground", "#ffffff")):
            self.root.option_add(f"*TCombobox*Listbox.{option}", value)
        self.root.option_add("*TCombobox*Listbox.font", FONT)

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=c["bg"], foreground=c["text"], font=FONT, bordercolor=c["border"],
                        focuscolor=c["bg"], troughcolor=c["surface"])
        style.configure("TFrame", background=c["bg"])
        style.configure("Card.TFrame", background=c["card"])
        style.configure("TLabel", background=c["bg"], foreground=c["text"])
        style.configure("Card.TLabel", background=c["card"])
        style.configure("Title.TLabel", font=FONT_TITLE)
        style.configure("CardTitle.TLabel", background=c["card"], foreground=c["muted"], font=FONT_SMALL_BOLD)
        style.configure("CardMuted.TLabel", background=c["card"], foreground=c["muted"])

        def button(name, bg, fg, hover, font=FONT):
            style.configure(name, background=bg, foreground=fg, font=font, borderwidth=0, relief="flat",
                            lightcolor=bg, darkcolor=bg, focuscolor=bg, padding=(px(12), px(8)))
            style.map(name,
                      background=[("disabled", c["disabled_bg"]), ("pressed", hover), ("active", hover)],
                      foreground=[("disabled", c["disabled_fg"])],
                      lightcolor=[("disabled", c["disabled_bg"]), ("active", hover)],
                      darkcolor=[("disabled", c["disabled_bg"]), ("active", hover)])

        button("TButton", c["surface"], c["text"], c["surface_hover"])
        button("Record.TButton", c["red"], "#ffffff", c["red_hover"], FONT_BOLD)
        button("Play.TButton", c["green"], "#0d1f17", c["green_hover"], FONT_BOLD)
        button("Ghost.TButton", c["bg"], c["muted"], c["surface"])

        style.configure("Lang.Toolbutton", background=c["surface"], foreground=c["muted"], font=FONT_SMALL_BOLD,
                        borderwidth=0, relief="flat", lightcolor=c["surface"], darkcolor=c["surface"],
                        focuscolor=c["surface"], padding=(px(10), px(4)))
        style.map("Lang.Toolbutton",
                  background=[("selected", c["blue"]), ("active", c["surface_hover"])],
                  foreground=[("selected", "#ffffff")],
                  lightcolor=[("selected", c["blue"]), ("active", c["surface_hover"])],
                  darkcolor=[("selected", c["blue"]), ("active", c["surface_hover"])])

        style.configure("TCombobox", fieldbackground=c["surface"], background=c["surface"], foreground=c["text"],
                        arrowcolor=c["text"], bordercolor=c["surface"], lightcolor=c["surface"],
                        darkcolor=c["surface"], padding=px(6))
        style.map("TCombobox",
                  fieldbackground=[("readonly", c["surface"])], foreground=[("readonly", c["text"])],
                  selectbackground=[("readonly", c["surface"])], selectforeground=[("readonly", c["text"])],
                  background=[("active", c["surface_hover"])])
        style.configure("Card.TCheckbutton", background=c["card"], foreground=c["text"], font=FONT,
                        indicatorbackground=c["surface"], indicatorforeground="#ffffff", focuscolor=c["card"],
                        bordercolor=c["border"], lightcolor=c["surface"], darkcolor=c["surface"])
        style.map("Card.TCheckbutton", background=[("active", c["card"])],
                  indicatorbackground=[("selected", c["blue"]), ("active", c["surface_hover"])])
        style.configure("TRadiobutton", background=c["bg"], foreground=c["text"], indicatorcolor=c["surface"])
        style.map("TRadiobutton", indicatorcolor=[("selected", c["blue"])], background=[("active", c["bg"])])
        style.configure("TEntry", fieldbackground=c["surface"], foreground=c["text"], insertcolor=c["text"],
                        bordercolor=c["surface"], lightcolor=c["surface"], darkcolor=c["surface"], padding=px(6))
        style.map("TEntry", fieldbackground=[("readonly", c["surface"])], foreground=[("readonly", c["text"])])

    def build_header(self, parent):
        header = ttk.Frame(parent)
        header.pack(fill=tk.X)
        self.title_label = ttk.Label(header, style="Title.TLabel")
        self.bind_text(self.title_label, "app_title")
        self.title_label.pack(side=tk.LEFT)

        switcher = ttk.Frame(header)
        switcher.pack(side=tk.RIGHT)
        self.lang_var = tk.StringVar(value=self.language)
        self.lang_buttons = {}
        for lang in ("ru", "en"):
            radio = ttk.Radiobutton(switcher, text=lang.upper(), value=lang, variable=self.lang_var,
                                    style="Lang.Toolbutton", command=lambda lang=lang: self.set_language(lang))
            radio.pack(side=tk.LEFT)
            self.lang_buttons[lang] = radio

    def build_status(self, parent):
        pill = ttk.Frame(parent, style="Card.TFrame", padding=(self.px(12), self.px(8)))
        pill.pack(fill=tk.X, pady=(self.px(12), self.px(12)))
        size = self.px(12)
        self.status_dot = tk.Canvas(pill, width=size, height=size, bg=COLORS["card"], highlightthickness=0)
        self.status_dot.create_oval(1, 1, size - 1, size - 1, fill=STATUS_COLORS["idle"], outline="", tags="dot")
        self.status_dot.pack(side=tk.LEFT, padx=(0, self.px(8)))
        self.status_label = ttk.Label(pill, style="Card.TLabel", font=FONT_BOLD)
        self.status_label.pack(side=tk.LEFT)

    def make_card(self, parent, title_key):
        card = ttk.Frame(parent, style="Card.TFrame", padding=self.px(14))
        card.pack(fill=tk.X, pady=(0, self.px(12)))
        title = ttk.Label(card, style="CardTitle.TLabel")
        self.bind_text(title, title_key, str.upper)
        title.pack(anchor=tk.W, pady=(0, self.px(8)))
        return card

    def make_button(self, parent, key, command, style="TButton", state=tk.NORMAL, grid=None):
        button = ttk.Button(parent, style=style, command=command, state=state)
        self.bind_text(button, key)
        if grid is None:
            button.pack(fill=tk.X, pady=self.px(3))
        else:
            row, column, padx = grid
            button.grid(row=row, column=column, sticky="ew", padx=padx)
        return button

    # ---------------------------------------------------------------- языки

    def t(self, key, **kwargs):
        return TRANSLATIONS[self.language][key].format(**kwargs)

    def bind_text(self, widget, key, transform=None):
        self._texts.append((widget, key, transform))
        widget.config(text=self.render_text(key, transform))

    def render_text(self, key, transform=None):
        text = self.t(key)
        return transform(text) if transform else text

    def set_language(self, language):
        self.language = language
        self.lang_var.set(language)
        self.root.title(self.t("app_title"))
        for widget, key, transform in self._texts:
            widget.config(text=self.render_text(key, transform))
        self.set_status(self.status_kind)
        self.render_countdown()
        self.update_recordings_menu()
        if self.hotkeys_window is not None and self.hotkeys_window.winfo_exists():
            self.render_hotkeys()
        if self.donate_window is not None and self.donate_window.winfo_exists():
            self.render_donate()

    def set_status(self, kind):
        self.status_kind = kind
        self.status_label.config(text=self.t(f"status_{kind}"))
        self.status_dot.itemconfig("dot", fill=STATUS_COLORS[kind])

    def render_countdown(self):
        state = self._countdown
        if state is None:
            text = ""
        elif state[0] == "wait":
            text = self.t("next_repeat", remaining=state[1], current=state[2], total=state[3])
        else:
            text = self.t("executing_repeat", current=state[1], total=state[2])
        self.countdown_label.config(text=text)

    # ------------------------------------------------- подсказка по клавишам

    def show_hotkeys(self):
        if self.hotkeys_window is not None and self.hotkeys_window.winfo_exists():
            self.hotkeys_window.lift()
            return
        window = tk.Toplevel(self.root)
        window.configure(bg=COLORS["bg"])
        window.transient(self.root)
        window.resizable(False, False)
        window.bind("<Escape>", lambda e: window.destroy())
        window.geometry(f"+{self.root.winfo_rootx() + self.px(40)}+{self.root.winfo_rooty() + self.px(60)}")
        self.hotkeys_window = window
        self.render_hotkeys()
        use_dark_title_bar(window)

    def render_hotkeys(self):
        window = self.hotkeys_window
        for child in window.winfo_children():
            child.destroy()
        window.title(self.t("hotkeys_title"))

        body = ttk.Frame(window, padding=self.px(16))
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=self.t("hotkeys_title"), style="Title.TLabel").pack(anchor=tk.W)

        table = ttk.Frame(body, style="Card.TFrame", padding=self.px(14))
        table.pack(fill=tk.X, pady=(self.px(12), self.px(8)))
        for row, (_sequence, display, desc_key) in enumerate(HOTKEYS):
            tk.Label(table, text=display, font=FONT_KEY, bg=COLORS["surface"], fg=COLORS["text"],
                     width=8, pady=self.px(3)).grid(row=row, column=0, sticky="w", pady=self.px(3))
            ttk.Label(table, text=self.t(desc_key), style="Card.TLabel").grid(
                row=row, column=1, sticky="w", padx=(self.px(12), 0), pady=self.px(3))

        ttk.Label(body, text=self.t("hotkeys_note"), foreground=COLORS["muted"],
                  wraplength=self.px(340)).pack(anchor=tk.W, pady=(0, self.px(12)))
        ttk.Button(body, text=self.t("close"), command=window.destroy).pack(anchor=tk.E)

    # ------------------------------------------------------------------ донаты

    def show_donate(self):
        if self.donate_window is not None and self.donate_window.winfo_exists():
            self.donate_window.lift()
            return
        self._reset_donate_copied()
        window = tk.Toplevel(self.root)
        window.configure(bg=COLORS["bg"])
        window.transient(self.root)
        window.resizable(False, False)
        window.bind("<Escape>", lambda e: window.destroy())
        self.donate_window = window
        self.render_donate()
        self.place_on_screen(window, self.root.winfo_rootx() + self.px(40), self.root.winfo_rooty() + self.px(60))
        use_dark_title_bar(window)
        window.focus_set()  # иначе Esc уходит в главное окно, а не закрывает это

    def place_on_screen(self, window, x, y):
        # Окно с QR высокое: сдвигаем его так, чтобы нижняя часть с кнопкой «Закрыть» не ушла за экран
        # (запас на заголовок окна и панель задач)
        window.update_idletasks()
        x = max(0, min(x, window.winfo_screenwidth() - window.winfo_reqwidth() - self.px(20)))
        y = max(0, min(y, window.winfo_screenheight() - window.winfo_reqheight() - self.px(90)))
        window.geometry(f"+{x}+{y}")

    def build_donation_qr(self, parent):
        matrix = donation_qr_matrix()
        if matrix is None:
            return None
        cell = self.px(5)
        canvas = tk.Canvas(parent, width=cell * len(matrix[0]), height=cell * len(matrix), bg="#ffffff",
                           highlightthickness=0)
        # Чёрные модули рисуем полосами по строке: меньше объектов и нет щелей между соседними
        for y, row in enumerate(matrix):
            x = 0
            while x < len(row):
                if not row[x]:
                    x += 1
                    continue
                start = x
                while x < len(row) and row[x]:
                    x += 1
                canvas.create_rectangle(start * cell, y * cell, x * cell, (y + 1) * cell,
                                        fill="#000000", outline="")
        return canvas

    def render_donate(self):
        window = self.donate_window
        for child in window.winfo_children():
            child.destroy()
        window.title(self.t("donate_title"))

        body = ttk.Frame(window, padding=self.px(16))
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=self.t("donate_title"), style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(body, text=self.t("donate_intro"), foreground=COLORS["muted"],
                  wraplength=self.px(400)).pack(anchor=tk.W, pady=(self.px(6), 0))

        card = ttk.Frame(body, style="Card.TFrame", padding=self.px(14))
        card.pack(fill=tk.X, pady=(self.px(12), self.px(8)))
        self.donate_qr = self.build_donation_qr(card)
        if self.donate_qr is not None:
            self.donate_qr.pack(pady=(0, self.px(12)))
        ttk.Label(card, text=self.t("donate_networks").upper(), style="CardTitle.TLabel").pack(anchor=tk.W)
        networks = f"{', '.join(DONATION_NETWORKS)} {self.t('donate_networks_more')}"
        ttk.Label(card, text=networks, style="Card.TLabel", wraplength=self.px(380)).pack(
            anchor=tk.W, pady=(self.px(4), self.px(10)))
        ttk.Label(card, text=self.t("donate_address").upper(), style="CardTitle.TLabel").pack(anchor=tk.W)

        entry = ttk.Entry(card, font=FONT_MONO, width=len(DONATION_ADDRESS) + 2)
        entry.insert(0, DONATION_ADDRESS)
        entry.config(state="readonly")
        entry.pack(fill=tk.X, pady=(self.px(4), self.px(8)))
        self.donate_copy_button = ttk.Button(card, command=self.copy_donation_address,
                                             text=self.t("donate_copied" if self._donate_copied else "donate_copy"))
        self.donate_copy_button.pack(fill=tk.X)

        ttk.Label(body, text=self.t("donate_warning"), foreground=COLORS["amber"],
                  wraplength=self.px(400)).pack(anchor=tk.W, pady=(0, self.px(12)))
        ttk.Button(body, text=self.t("close"), command=window.destroy).pack(anchor=tk.E)

    def copy_donation_address(self):
        if not set_clipboard_text(self.root, DONATION_ADDRESS):
            self.root.clipboard_clear()
            self.root.clipboard_append(DONATION_ADDRESS)
        self._donate_copied = True
        if self._donate_reset_timer is not None:
            self.root.after_cancel(self._donate_reset_timer)
        self._donate_reset_timer = self.root.after(2000, self._reset_donate_copied)
        self.donate_copy_button.config(text=self.t("donate_copied"))

    def _reset_donate_copied(self):
        if self._donate_reset_timer is not None:
            self.root.after_cancel(self._donate_reset_timer)
            self._donate_reset_timer = None
        self._donate_copied = False
        if self.donate_window is not None and self.donate_window.winfo_exists():
            self.donate_copy_button.config(text=self.t("donate_copy"))

    # -------------------------------------------------------- запись / список

    def update_recordings_menu(self):
        placeholder = self.t("no_recordings")
        self.recording_menu.config(values=self.recordings or [placeholder])
        self.recording_var.set(self.recordings[-1] if self.recordings else placeholder)

    def recording_button_areas(self):
        areas = []
        for button in (self.record_button, self.stop_recording_button):
            left, top = button.winfo_rootx(), button.winfo_rooty()
            areas.append((left, top, left + button.winfo_width(), top + button.winfo_height()))
        return areas

    def start_recording(self):
        self.recorder.start_recording()
        self.record_button.config(state=tk.DISABLED)
        self.stop_recording_button.config(state=tk.NORMAL)
        self.play_button.config(state=tk.DISABLED)
        self.save_button.config(state=tk.DISABLED)
        self.stop_playback_button.config(state=tk.DISABLED)
        self.set_status("recording")

    def stop_recording(self):
        self.recorder.stop_recording(self.recording_button_areas())
        self.record_button.config(state=tk.NORMAL)
        self.stop_recording_button.config(state=tk.DISABLED)
        self.play_button.config(state=tk.NORMAL)
        self.save_button.config(state=tk.NORMAL)
        self.set_status("idle")

    # ---------------------------------------------------------- диалоги

    def new_dialog(self, title):
        window = tk.Toplevel(self.root)
        window.title(title)
        window.configure(bg=COLORS["bg"])
        window.transient(self.root)
        window.resizable(False, False)
        window.geometry(f"+{self.root.winfo_rootx() + self.px(30)}+{self.root.winfo_rooty() + self.px(50)}")
        body = ttk.Frame(window, padding=self.px(16))
        body.pack(fill=tk.BOTH, expand=True)
        use_dark_title_bar(window)
        return window, body

    def choose_interval_method(self):
        interval_window, body = self.new_dialog(self.t("interval_method_title"))

        chosen_method = tk.StringVar(value="manual")

        manual_radio = ttk.Radiobutton(body, text=self.t("manual"), variable=chosen_method, value="manual")
        manual_radio.grid(row=0, column=0, padx=self.px(10), pady=self.px(10))
        random_radio = ttk.Radiobutton(body, text=self.t("random"), variable=chosen_method, value="random")
        random_radio.grid(row=0, column=1, padx=self.px(10), pady=self.px(10))

        def on_ok():
            method = chosen_method.get()
            interval_window.destroy()
            if method == "manual":
                self.prompt_manual_intervals()
            elif method == "random":
                self.prompt_random_intervals()

        ok_button = ttk.Button(body, text=self.t("ok"), command=on_ok)
        ok_button.grid(row=1, column=0, columnspan=2, pady=self.px(10))

    def prompt_manual_intervals(self):
        repeat_count = simpledialog.askinteger(self.t("repeat_count_title"), self.t("repeat_count_prompt"),
                                               minvalue=1, initialvalue=1)
        if repeat_count is None:
            return

        intervals = []

        def show_interval_prompt(index):
            interval_window, body = self.new_dialog(self.t("interval_title", n=index + 1))

            label = ttk.Label(body, text=self.t("interval_prompt", n=index + 1))
            label.grid(row=0, column=0, padx=self.px(10), pady=self.px(10))

            entry = ttk.Entry(body)
            entry.grid(row=0, column=1, padx=self.px(10), pady=self.px(10))
            entry.focus_set()

            def set_same_for_all():
                try:
                    current_value = float(entry.get())
                except ValueError:
                    messagebox.showerror(self.t("invalid_title"), self.t("invalid_number"))
                    return
                for i in range(index, repeat_count):
                    intervals.append(current_value)
                interval_window.destroy()
                self.start_playback_if_not_playing(intervals)

            def on_ok():
                try:
                    interval = float(entry.get())
                except ValueError:
                    messagebox.showerror(self.t("invalid_title"), self.t("invalid_number"))
                    return
                intervals.append(interval)
                interval_window.destroy()
                if len(intervals) < repeat_count:
                    show_interval_prompt(len(intervals))
                else:
                    self.start_playback_if_not_playing(intervals)

            def on_cancel():
                interval_window.destroy()

            ok_button = ttk.Button(body, text=self.t("ok"), command=on_ok)
            ok_button.grid(row=1, column=0, padx=self.px(6), pady=self.px(10))

            set_same_button = ttk.Button(body, text=self.t("set_same_for_all"), command=set_same_for_all)
            set_same_button.grid(row=1, column=1, padx=self.px(6), pady=self.px(10))

            cancel_button = ttk.Button(body, text=self.t("cancel"), command=on_cancel)
            cancel_button.grid(row=1, column=2, padx=self.px(6), pady=self.px(10))

        show_interval_prompt(0)

    def prompt_random_intervals(self):
        min_interval = simpledialog.askfloat(self.t("min_interval_title"), self.t("min_interval_prompt"),
                                             minvalue=0.0, initialvalue=0.0)
        if min_interval is None:
            return
        max_interval = simpledialog.askfloat(self.t("max_interval_title"), self.t("max_interval_prompt"),
                                             minvalue=min_interval, initialvalue=min_interval)
        if max_interval is None:
            return
        repeat_count = simpledialog.askinteger(self.t("repeat_count_title"), self.t("repeat_count_prompt"),
                                               minvalue=1, initialvalue=1)
        if repeat_count is None:
            return

        intervals = [random.uniform(min_interval, max_interval) for _ in range(repeat_count)]
        self.start_playback_if_not_playing(intervals)

    # ------------------------------------------------------- воспроизведение

    def start_playback_if_not_playing(self, intervals):
        if not self.recorder.playing:
            self.recorder.game_mode = self.game_mode_var.get()
            self.recorder.start_playback(intervals)
            self.stop_playback_button.config(state=tk.NORMAL)
            self.set_status("playing")

    def stop_playback(self):
        self.recorder.stop_playback()
        self.stop_playback_button.config(state=tk.DISABLED)
        self.set_status("stopping")

        # Запускаем отдельный поток для ожидания завершения воспроизведения
        threading.Thread(target=self.wait_for_playback_stop, daemon=True).start()

    def wait_for_playback_stop(self):
        if self.recorder.playback_thread:
            self.recorder.playback_thread.join(timeout=1)  # Ждем не более 1 секунды

        # Обновляем GUI в главном потоке
        self.root.after(0, self.update_gui_after_stop)

    def update_gui_after_stop(self):
        self.set_status("idle")
        self._countdown = None
        self.render_countdown()

    def save_events(self):
        file_path = filedialog.asksaveasfilename(defaultextension=".json",
                                                 filetypes=[(self.t("json_files"), "*.json")])
        if file_path:
            with open(file_path, "w") as f:
                json.dump(self.recorder.events, f)
            if os.path.basename(file_path) not in self.recordings:
                self.recordings.append(os.path.basename(file_path))
            self.update_recordings_menu()

    def load_events(self):
        file_path = filedialog.askopenfilename(defaultextension=".json",
                                               filetypes=[(self.t("json_files"), "*.json")])
        if file_path:
            with open(file_path, "r") as f:
                self.recorder.events = json.load(f)
            if os.path.basename(file_path) not in self.recordings:
                self.recordings.append(os.path.basename(file_path))
            self.update_recordings_menu()
            self.recording_var.set(os.path.basename(file_path))
            self.play_button.config(state=tk.NORMAL)

    def update_gui(self, remaining_time, current_repeat, total_repeats):
        if remaining_time > 0:
            self._countdown = ("wait", remaining_time, current_repeat, total_repeats)
        elif current_repeat == 0:
            self.set_status("idle")
            self._countdown = None
        else:
            self._countdown = ("exec", current_repeat, total_repeats)
        self.render_countdown()
        self.root.update()


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
