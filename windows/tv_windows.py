#!/usr/bin/env python3
"""
Windows version of silence-refresh.py: same core behaviour, but Chrome only and
with a small tkinter GUI (pick site and channel, press "Guarda") instead of
shell aliases.

Requirements: see requirements.txt; Google Chrome must be installed.

Not tested on real Windows hardware (only built in CI): WASAPI loopback
capture and pycaw mute detection may need adjustments.
"""

import ctypes
import os
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import numpy as np

try:
    import sounddevice as sd
except OSError as exc:
    raise SystemExit(
        "Impossibile caricare PortAudio (sounddevice). Prova a reinstallare "
        f"le dipendenze da requirements.txt.\nDettaglio: {exc}"
    )

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# --- Logging to the console and, if registered, to the GUI (the packaged
# exe has no console). ---

_log_callback = None


def set_log_callback(fn) -> None:
    global _log_callback
    _log_callback = fn


def log(msg: str) -> None:
    print(msg)
    if _log_callback:
        try:
            _log_callback(msg)
        except Exception:
            pass


# --- Per-site cookie banners / play buttons (same as the Linux version). ---

def accept_cookies_mediaset(driver, timeout: float = 8.0) -> None:
    """Mediaset Infinity cookie banner: open the detailed panel and accept
    only strictly necessary cookies."""
    try:
        open_modal_btn = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.ID, "rti-privacy-open-modal-btn-id"))
        )
    except TimeoutException:
        return
    open_modal_btn.click()
    try:
        necessary_only_btn = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.ID, "rti-privacy-close-btn-id"))
        )
        necessary_only_btn.click()
        log("Cookie: accettati solo i necessari.")
    except TimeoutException:
        log("Attenzione: banner cookie aperto ma bottone 'solo necessari' non trovato.")


def accept_cookies_rai(driver, timeout: float = 8.0) -> None:
    """RaiPlay cookie banner (Avacy): "continue without accepting" rejects
    all non-essential cookies in one click."""
    try:
        btn = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".as-js-close-banner"))
        )
        btn.click()
        log("Cookie: consenso rifiutato (solo necessari).")
    except TimeoutException:
        return


def accept_cookies_tv8(driver, timeout: float = 8.0) -> None:
    """TV8 cookie banner: a Sourcepoint CMP inside a cross-origin iframe
    (cmp.sky.it). "Continue without accepting" rejects non-essential
    cookies."""
    try:
        WebDriverWait(driver, timeout).until(
            EC.frame_to_be_available_and_switch_to_it((By.CSS_SELECTOR, "iframe[src*='cmp.sky.it']"))
        )
        btn = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".sp_choice_type_13"))
        )
        btn.click()
        log("Cookie: consenso rifiutato (solo necessari).")
    except TimeoutException:
        pass
    finally:
        driver.switch_to.default_content()


def click_play_rai(driver, timeout: float = 15.0) -> None:
    """On RaiPlay the <video> element only appears after pressing the
    play button, so click it before waiting for the video."""
    try:
        btn = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, "//*[@aria-label='Riproduci']"))
        )
        btn.click()
        log("Player: cliccato 'Riproduci'.")
    except TimeoutException:
        pass


def accept_cookies_nove(driver, timeout: float = 8.0) -> None:
    """Nove cookie banner (OneTrust): open the preferences panel, then
    choose "essential cookies only"."""
    try:
        prefs_btn = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.ID, "onetrust-pc-btn-handler"))
        )
    except TimeoutException:
        return
    # A native click fails ("could not be scrolled into view") because the
    # banner is position: fixed; a JS click skips scrolling.
    driver.execute_script("arguments[0].click();", prefs_btn)
    try:
        only_essential_btn = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(text(),'Solo cookie essenziali')]"))
        )
        only_essential_btn.click()
        log("Cookie: accettati solo i necessari.")
    except TimeoutException:
        log("Attenzione: pannello preferenze cookie Nove aperto ma bottone 'solo essenziali' non trovato.")


def click_play_nove(driver, timeout: float = 15.0) -> None:
    """The Nove player (Video.js) needs its big play button clicked. It
    starts muted; ensure_unmuted() unmutes it afterwards."""
    try:
        btn = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".vjs-big-play-button"))
        )
        btn.click()
        log("Player: cliccato 'Play'.")
    except TimeoutException:
        return


def accept_cookies_la7(driver, timeout: float = 8.0) -> None:
    """La7 cookie banner (IAB TCF2): click the first-level reject button,
    matched case-insensitively on "reject" or "senza accettare" since the
    label language varies. Never open "Preferences": that panel hangs the
    automation."""
    try:
        btn = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//*[self::button or self::a or self::div or self::span]"
                "[contains(translate(text(), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'reject')"
                " or contains(translate(text(), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'senza accettare')]",
            ))
        )
        btn.click()
        log("Cookie: consenso rifiutato (solo necessari).")
    except TimeoutException:
        return


# --- Sites/channels (Linux list minus Discovery; "label" is for the GUI). ---

SITES = {
    "mediaset": {
        "label": "Mediaset Infinity",
        "base_url": "https://mediasetinfinity.mediaset.it/diretta/{slug}",
        "channels": {
            "canale5": "canale5_cC5",
            "italia1": "italia1_cI1",
            "italia2": "italia2_cI2",
            "rete4": "rete4_cR4",
            "20mediaset": "20mediaset_cLB",
            "iris": "iris_cKI",
            "la5": "la5_cKA",
            "cine34": "cine34_cB6",
            "focus": "focus_cFU",
            "topcrime": "topcrime_cLT",
            "mediasetextra": "mediasetextra_cKQ",
            "tgcom24": "video_cKF",
        },
        "profile_dirname": "chrome-mediaset-profile",
        "accept_cookies": accept_cookies_mediaset,
        "click_play": None,
    },
    "rai": {
        "label": "RaiPlay",
        "base_url": "https://www.raiplay.it/dirette/{slug}",
        "channels": {
            "rai1": "rai1",
            "rai2": "rai2",
            "rai3": "rai3",
            "rai4": "rai4",
            "rai5": "rai5",
            "raigulp": "raigulp",
            "raimovie": "raimovie",
            "rainews24": "rainews24",
            "raipremium": "raipremium",
            "rairadio2": "rairadio2",
            "raiscuola": "raiscuola",
            "raisport": "raisport",
            "raistoria": "raistoria",
            "raiyoyo": "raiyoyo",
        },
        "profile_dirname": "chrome-rai-profile",
        "accept_cookies": accept_cookies_rai,
        "click_play": click_play_rai,
    },
    "la7": {
        "label": "La7",
        "base_url": "https://www.la7.it/{slug}",
        "channels": {"la7": "dirette-tv"},
        "profile_dirname": "chrome-la7-profile",
        "accept_cookies": accept_cookies_la7,
        "click_play": None,
    },
    "tv8": {
        "label": "TV8",
        "base_url": "https://www.tv8.it/{slug}",
        "channels": {"tv8": "streaming"},
        "profile_dirname": "chrome-tv8-profile",
        "accept_cookies": accept_cookies_tv8,
        "click_play": None,
    },
    "nove": {
        "label": "Nove",
        "base_url": "https://nove.tv/{slug}",
        "channels": {"nove": "live-streaming-nove"},
        "profile_dirname": "chrome-nove-profile",
        "accept_cookies": accept_cookies_nove,
        "click_play": click_play_nove,
    },
}


# --- Windows-specific helpers (replace xrandr/pactl). ---

def get_screen_resolution() -> tuple[int, int] | None:
    """Physical primary screen resolution, used by is_fullscreen().
    Requires DPI awareness (set in main()), otherwise display scaling
    skews the value."""
    try:
        user32 = ctypes.windll.user32
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    except Exception:
        return None


def is_system_output_muted() -> bool:
    """True if the default output device is muted or at 0% volume (pycaw):
    the silence is then not the stream's fault, so reloads are paused."""
    try:
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        if volume.GetMute():
            return True
        if volume.GetMasterVolumeLevelScalar() <= 0.0:
            return True
    except Exception:
        pass
    return False


def find_chrome_binary() -> str | None:
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None  # Selenium Manager will still look for it


def find_loopback_device() -> tuple[int, dict]:
    """Default output device, to be opened as a WASAPI loopback
    (WasapiSettings(loopback=True)) to capture what the speakers play."""
    host_apis = sd.query_hostapis()
    wasapi_idx = next(
        (i for i, h in enumerate(host_apis) if "wasapi" in h["name"].lower()), None
    )
    if wasapi_idx is None:
        raise RuntimeError("Host API WASAPI non trovata su questo sistema.")
    default_output = host_apis[wasapi_idx]["default_output_device"]
    if default_output is None or default_output < 0:
        raise RuntimeError("Nessun dispositivo di output audio di default trovato.")
    device_info = sd.query_devices(default_output)
    return default_output, device_info


# --- Shared logic (same as the Linux version). ---

def rms_dbfs(samples: np.ndarray) -> float:
    rms = np.sqrt(np.mean(np.square(samples), dtype=np.float64))
    if rms <= 1e-9:
        return -120.0
    return 20 * np.log10(rms)


def wait_for_video_element(driver, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            driver.find_element("tag name", "video")
            return True
        except NoSuchElementException:
            time.sleep(1)
    return False


def is_fullscreen(driver, screen_resolution: tuple[int, int] | None) -> bool:
    """Detect both browser/window fullscreen (window size equals the
    screen resolution) and player fullscreen (Fullscreen API)."""
    try:
        if driver.execute_script("return !!document.fullscreenElement;"):
            return True
        if screen_resolution is None:
            return False
        dims = driver.execute_script("return [window.outerWidth, window.outerHeight];")
        return tuple(dims) == screen_resolution
    except WebDriverException:
        return False


def move_cursor_away(driver) -> None:
    """Move the pointer to the top-left corner so it doesn't sit over the
    video in fullscreen. W3C action offsets are relative to the element's
    center, hence the computation."""
    try:
        width, height = driver.execute_script("return [window.innerWidth, window.innerHeight];")
        body = driver.find_element(By.TAG_NAME, "body")
        ActionChains(driver).move_to_element_with_offset(
            body, int(1 - width / 2), int(1 - height / 2)
        ).perform()
    except WebDriverException:
        pass


def ensure_unmuted(driver) -> None:
    """Some players start muted because of autoplay policies: unmute every
    <video> on the page."""
    try:
        driver.execute_script(
            "document.querySelectorAll('video').forEach(function (v) { "
            "v.muted = false; v.volume = 1.0; });"
        )
    except WebDriverException:
        pass


def load_page(driver, url: str, site_cfg: dict) -> None:
    driver.get(url)
    accept_cookies = site_cfg.get("accept_cookies")
    if accept_cookies:
        accept_cookies(driver)
    click_play = site_cfg.get("click_play")
    if click_play:
        click_play(driver)
    if wait_for_video_element(driver, timeout=30):
        time.sleep(1.5)
        ensure_unmuted(driver)
    else:
        log("Attenzione: nessun tag <video> individuato entro 30s (potrebbe essere in un iframe o dietro un consenso cookie). Proseguo comunque.")


# --- Chrome launch + monitoring loop (the Linux main(), driven by the GUI). ---

PROFILES_BASE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TV"

SILENCE_THRESHOLD_DB = -50.0
SILENCE_SECONDS = 5.0
STARTUP_GRACE_SECONDS = 12.0
CHECK_INTERVAL = 0.5


def run_channel(site_key: str, channel: str, stop_event: threading.Event) -> None:
    site_cfg = SITES[site_key]
    slug = site_cfg["channels"][channel]
    url = slug if slug.startswith("http") else site_cfg["base_url"].format(slug=slug)

    binary = find_chrome_binary()
    profile_dir = PROFILES_BASE / site_cfg["profile_dirname"]
    profile_dir.mkdir(parents=True, exist_ok=True)

    options = ChromeOptions()
    if binary:
        options.binary_location = binary
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--no-first-run")

    log(f"Avvio Chrome su {url}")
    driver = webdriver.Chrome(options=options)
    driver.maximize_window()

    try:
        device, device_info = find_loopback_device()
        samplerate = int(device_info["default_samplerate"])
    except Exception as exc:
        log(f"Attenzione: cattura audio non disponibile ({exc}). Il canale resta aperto ma senza reload automatico sul silenzio.")
        device = None
        samplerate = None

    try:
        load_page(driver, url, site_cfg)
        stream_ready_at = time.monotonic() + STARTUP_GRACE_SECONDS

        screen_resolution = get_screen_resolution()
        fullscreen_state = is_fullscreen(driver, screen_resolution)
        next_fullscreen_check = time.monotonic() + 1.0
        system_muted = is_system_output_muted()
        next_mute_check = time.monotonic() + 1.0
        if system_muted:
            log("Attenzione: volume di sistema disattivato/a zero. Il controllo silenzio resta in pausa finche' non lo riattivi.")

        if device is None:
            # No audio capture: just wait for stop_event.
            while not stop_event.is_set():
                time.sleep(0.5)
            return

        silence_started = None
        block_size = max(1, int(samplerate * CHECK_INTERVAL))
        wasapi_settings = sd.WasapiSettings(loopback=True)

        log(f"Monitoraggio audio di sistema su device #{device} ({device_info['name']}, {samplerate} Hz)")
        log(f"Soglia silenzio: {SILENCE_THRESHOLD_DB} dBFS per {SILENCE_SECONDS}s -> ricarico la pagina")

        with sd.InputStream(
            device=device, channels=device_info["max_output_channels"] or 2,
            samplerate=samplerate, blocksize=block_size, extra_settings=wasapi_settings,
        ) as stream:
            while not stop_event.is_set():
                samples, _ = stream.read(block_size)
                level_db = rms_dbfs(samples[:, 0])
                now = time.monotonic()

                if now < stream_ready_at:
                    continue

                if now >= next_mute_check:
                    next_mute_check = now + 1.0
                    newly_muted = is_system_output_muted()
                    if newly_muted and not system_muted:
                        log(f"[{time.strftime('%H:%M:%S')}] Volume di sistema disattivato: metto in pausa il controllo silenzio finche' non lo riattivi.")
                    elif system_muted and not newly_muted:
                        log(f"[{time.strftime('%H:%M:%S')}] Volume di sistema riattivato, riprendo il controllo silenzio.")
                    system_muted = newly_muted

                if system_muted:
                    silence_started = None
                elif level_db < SILENCE_THRESHOLD_DB:
                    if silence_started is None:
                        silence_started = now
                    elapsed = now - silence_started
                    if elapsed >= SILENCE_SECONDS:
                        log(f"[{time.strftime('%H:%M:%S')}] Silenzio da {elapsed:.0f}s ({level_db:.1f} dBFS) -> reload pagina")
                        was_fullscreen = is_fullscreen(driver, screen_resolution)
                        try:
                            driver.get(url)
                            accept_cookies = site_cfg.get("accept_cookies")
                            if accept_cookies:
                                accept_cookies(driver)
                            click_play = site_cfg.get("click_play")
                            if click_play:
                                click_play(driver)
                            if wait_for_video_element(driver, timeout=30):
                                time.sleep(1.5)
                                ensure_unmuted(driver)
                            if was_fullscreen:
                                driver.fullscreen_window()
                                move_cursor_away(driver)
                                log("Schermo intero ripristinato.")
                                fullscreen_state = True
                                next_fullscreen_check = time.monotonic() + 1.0
                        except WebDriverException as exc:
                            log(f"Errore durante il reload: {exc}")
                        silence_started = None
                        stream_ready_at = time.monotonic() + STARTUP_GRACE_SECONDS
                else:
                    silence_started = None

                if now >= next_fullscreen_check:
                    next_fullscreen_check = now + 1.0
                    currently_fullscreen = is_fullscreen(driver, screen_resolution)
                    if currently_fullscreen and not fullscreen_state:
                        move_cursor_away(driver)
                    fullscreen_state = currently_fullscreen
    except WebDriverException as exc:
        log(f"Errore: {exc}")
    finally:
        log("Chiudo il browser.")
        driver.quit()


# --- GUI. ---

class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("TV - Dirette streaming")
        root.geometry("560x420")

        self.stop_event: threading.Event | None = None
        self.worker: threading.Thread | None = None

        self.picker_frame = ttk.Frame(root, padding=10)
        self.picker_frame.pack(fill="both", expand=True)

        ttk.Label(self.picker_frame, text="Sito:").grid(row=0, column=0, sticky="w")
        self.site_list = tk.Listbox(self.picker_frame, height=6, exportselection=False)
        for key, cfg in SITES.items():
            self.site_list.insert("end", cfg["label"])
        self.site_list.grid(row=1, column=0, sticky="ns", padx=(0, 10))
        self.site_list.bind("<<ListboxSelect>>", self._on_site_selected)

        ttk.Label(self.picker_frame, text="Canale:").grid(row=0, column=1, sticky="w")
        self.channel_list = tk.Listbox(self.picker_frame, height=14, exportselection=False)
        self.channel_list.grid(row=1, column=1, sticky="nsew")
        self.channel_list.bind("<Double-Button-1>", lambda e: self._start())

        self.picker_frame.columnconfigure(1, weight=1)
        self.picker_frame.rowconfigure(1, weight=1)

        self.watch_btn = ttk.Button(self.picker_frame, text="Guarda", command=self._start)
        self.watch_btn.grid(row=2, column=0, columnspan=2, pady=10, sticky="ew")

        self.log_frame = ttk.Frame(root, padding=10)
        self.log_text = tk.Text(self.log_frame, height=18, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.stop_btn = ttk.Button(self.log_frame, text="Ferma e torna alla lista", command=self._stop)
        self.stop_btn.pack(pady=(10, 0), fill="x")

        self._site_keys = list(SITES.keys())
        self.site_list.selection_set(0)
        self._on_site_selected()

        set_log_callback(self._append_log)

    def _on_site_selected(self, event=None) -> None:
        sel = self.site_list.curselection()
        if not sel:
            return
        site_key = self._site_keys[sel[0]]
        self.channel_list.delete(0, "end")
        self._channel_keys = list(SITES[site_key]["channels"].keys())
        for ch in self._channel_keys:
            self.channel_list.insert("end", ch)
        self.channel_list.selection_set(0)

    def _append_log(self, msg: str) -> None:
        def _do():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(0, _do)

    def _start(self) -> None:
        site_sel = self.site_list.curselection()
        chan_sel = self.channel_list.curselection()
        if not site_sel or not chan_sel:
            return
        site_key = self._site_keys[site_sel[0]]
        channel = self._channel_keys[chan_sel[0]]

        self.picker_frame.pack_forget()
        self.log_frame.pack(fill="both", expand=True)
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

        self.stop_event = threading.Event()
        self.worker = threading.Thread(
            target=run_channel, args=(site_key, channel, self.stop_event), daemon=True
        )
        self.worker.start()

    def _stop(self) -> None:
        if self.stop_event:
            self.stop_event.set()
        self.log_frame.pack_forget()
        self.picker_frame.pack(fill="both", expand=True)


def main() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
