#!/usr/bin/env python3
"""
Open an Italian live TV channel in a browser (via Selenium) and reload the
page when the system audio stays silent for too long.

Silence is measured on the system loopback (PulseAudio/PipeWire sink
monitor), i.e. after decoding, so it also works with DRM-protected streams
where in-page Web Audio capture is blocked.

Requirements: LibreWolf (native package) + geckodriver, Google Chrome (for
la7), portaudio19-dev, and the Python packages selenium, sounddevice, numpy.
See setup.sh.

The whole system output is monitored: keep other audio sources quiet while
it runs.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

# On PipeWire, PortAudio exposes no "Monitor of ..." device: use the ALSA
# "pulse" device and select the monitor source here. Must be set before the
# stream is opened.
os.environ.setdefault("PULSE_SOURCE", "@DEFAULT_SINK@.monitor")

try:
    import sounddevice as sd
except OSError as exc:
    sys.exit(
        "Impossibile caricare PortAudio. Installa la libreria di sistema:\n"
        "  sudo apt install portaudio19-dev\n"
        f"Dettaglio: {exc}"
    )

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


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
        print("Cookie: accettati solo i necessari.")
    except TimeoutException:
        print("Attenzione: banner cookie aperto ma bottone 'solo necessari' non trovato.")


def accept_cookies_rai(driver, timeout: float = 8.0) -> None:
    """RaiPlay cookie banner (Avacy): "continue without accepting" rejects
    all non-essential cookies in one click."""
    try:
        btn = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".as-js-close-banner"))
        )
        btn.click()
        print("Cookie: consenso rifiutato (solo necessari).")
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
        print("Cookie: consenso rifiutato (solo necessari).")
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
        print("Player: cliccato 'Riproduci'.")
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
        print("Cookie: accettati solo i necessari.")
    except TimeoutException:
        print("Attenzione: pannello preferenze cookie Nove aperto ma bottone 'solo essenziali' non trovato.")


def click_play_nove(driver, timeout: float = 15.0) -> None:
    """The Nove player (Video.js) needs its big play button clicked. It
    starts muted; ensure_unmuted() unmutes it afterwards."""
    try:
        btn = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".vjs-big-play-button"))
        )
        btn.click()
        print("Player: cliccato 'Play'.")
    except TimeoutException:
        return


FULLSCREEN_CONTROL_SELECTORS = (
    ".vjs-fullscreen-control",   # Video.js (Nove, Discovery, Mediaset)
    ".jw-icon-fullscreen",       # JW Player (TV8)
)


def enter_player_fullscreen(driver, timeout: float = 10.0) -> bool:
    """Put the player (not just the window) back in fullscreen after a
    reload, using a real Selenium input as the required user gesture: click
    the player's fullscreen button, or double-click the video as a fallback.
    Returns True only if fullscreen is actually active (see DESIGN.md)."""
    try:
        video = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.TAG_NAME, "video"))
        )
    except TimeoutException:
        return False
    # Hover the video so the (auto-hidden) control bar shows up.
    try:
        ActionChains(driver).move_to_element(video).perform()
    except WebDriverException:
        pass
    clicked = False
    for selector in FULLSCREEN_CONTROL_SELECTORS:
        try:
            btn = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
            )
            btn.click()
            clicked = True
            break
        except (TimeoutException, WebDriverException):
            continue
    if not clicked:
        # No button found: double-click toggles fullscreen in most players.
        try:
            ActionChains(driver).double_click(video).perform()
        except WebDriverException:
            return False
    try:
        WebDriverWait(driver, 3).until(
            lambda d: d.execute_script("return !!document.fullscreenElement;")
        )
        return True
    except (TimeoutException, WebDriverException):
        return False


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
        print("Cookie: consenso rifiutato (solo necessari).")
    except TimeoutException:
        return


# Channel slugs/URLs taken from each site. If a channel stops working,
# open its live page and copy the new slug from the address bar.
SITES = {
    "mediaset": {
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
        "profile_dirname": ".librewolf-mediaset-profile",
        "accept_cookies": accept_cookies_mediaset,
        "click_play": None,
    },
    "rai": {
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
        "profile_dirname": ".librewolf-rai-profile",
        "accept_cookies": accept_cookies_rai,
        "click_play": click_play_rai,
    },
    # La7: the real player is on /dirette-tv (the homepage only has a muted
    # preview). The stream uses Widevine, which LibreWolf does not ship, so
    # Chrome is used; its autoplay flag (see main) starts it with audio.
    "la7": {
        "base_url": "https://www.la7.it/{slug}",
        "channels": {"la7": "dirette-tv"},
        "profile_dirname": ".chrome-la7-profile",
        "browser": "chrome",
        "accept_cookies": accept_cookies_la7,
        "click_play": None,
    },
    "tv8": {
        "base_url": "https://www.tv8.it/{slug}",
        "channels": {"tv8": "streaming"},
        "profile_dirname": ".librewolf-tv8-profile",
        "accept_cookies": accept_cookies_tv8,
        "click_play": None,
    },
    # Nove: the official site is nove.tv (not nove.it). With a VPN active
    # the stream stays stuck loading (likely geo-blocking).
    "nove": {
        "base_url": "https://nove.tv/{slug}",
        "channels": {"nove": "live-streaming-nove"},
        "profile_dirname": ".librewolf-nove-profile",
        "accept_cookies": accept_cookies_nove,
        "click_play": click_play_nove,
    },
    # Warner Bros. Discovery channels: same platform as Nove (same cookie
    # banner and player), but each on its own domain, so the "slug" is the
    # full URL. Same VPN caveat as Nove.
    "discovery": {
        "base_url": "{slug}",
        "channels": {
            "dmax": "https://dmax.it/live-streaming-dmax",
            "realtime": "https://realtime.it/live-streaming-real-time",
            "giallo": "https://giallotv.it/live-streaming-giallo",
            "motortrend": "https://discoveryturbo.it/live-streaming-motor-trend",
            "foodnetwork": "https://foodnetwork.it/live-streaming-food-network",
        },
        "profile_dirname": ".librewolf-discovery-profile",
        "accept_cookies": accept_cookies_nove,
        "click_play": click_play_nove,
    },
}


def find_librewolf_binary(explicit_path: str | None) -> str:
    if explicit_path:
        if not Path(explicit_path).is_file():
            sys.exit(f"Binario LibreWolf non trovato: {explicit_path}")
        return explicit_path
    found = shutil.which("librewolf")
    if found:
        return found
    for candidate in ("/usr/bin/librewolf", "/usr/local/bin/librewolf", "/opt/librewolf/librewolf"):
        if Path(candidate).is_file():
            return candidate
    sys.exit(
        "LibreWolf non trovato nel PATH. Installa il pacchetto nativo "
        "oppure indica il percorso con --librewolf-path."
    )


def find_chrome_binary(explicit_path: str | None) -> str:
    if explicit_path:
        if not Path(explicit_path).is_file():
            sys.exit(f"Binario Chrome non trovato: {explicit_path}")
        return explicit_path
    found = shutil.which("google-chrome")
    if found:
        return found
    for candidate in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/opt/google/chrome/google-chrome"):
        if Path(candidate).is_file():
            return candidate
    sys.exit(
        "Google Chrome non trovato nel PATH. Installa il pacchetto (necessario per i siti "
        "con DRM/Widevine non supportato da LibreWolf) oppure indica il percorso con --chrome-path."
    )


def find_monitor_device(explicit_device: str | None):
    if explicit_device:
        return explicit_device
    devices = sd.query_devices()
    for idx, dev in enumerate(devices):
        name = dev.get("name", "")
        if dev.get("max_input_channels", 0) > 0 and "monitor" in name.lower():
            return idx
    # PipeWire: fall back to the ALSA "pulse" device (see PULSE_SOURCE above).
    for idx, dev in enumerate(devices):
        if dev.get("name", "").lower() == "pulse" and dev.get("max_input_channels", 0) > 0:
            return idx
    sys.exit(
        "Nessun device di loopback trovato (ne' 'Monitor of ...' ne' 'pulse').\n"
        "Elenca i device disponibili con:\n"
        "  python3 -c \"import sounddevice as sd; print(sd.query_devices())\"\n"
        "e passa quello giusto con --audio-device <indice_o_nome>."
    )


def rms_dbfs(samples: np.ndarray) -> float:
    rms = np.sqrt(np.mean(np.square(samples), dtype=np.float64))
    if rms <= 1e-9:
        return -120.0
    return 20 * np.log10(rms)


def is_system_output_muted() -> bool:
    """True if the default sink is muted or at 0% volume: the silence is
    then not the stream's fault, so reloads are paused."""
    try:
        mute_out = subprocess.run(
            ["pactl", "get-sink-mute", "@DEFAULT_SINK@"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        if "yes" in mute_out.lower():
            return True
        vol_out = subprocess.run(
            ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        m = re.search(r"(\d+)%", vol_out)
        if m and int(m.group(1)) == 0:
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def wait_for_video_element(driver, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            driver.find_element("tag name", "video")
            return True
        except NoSuchElementException:
            time.sleep(1)
    return False


def get_screen_resolution() -> tuple[int, int] | None:
    """Physical screen resolution from xrandr, used by is_fullscreen().
    On Firefox/Wayland window.screen follows the window size, so it can't
    be used."""
    try:
        out = subprocess.run(
            ["xrandr", "--query"], capture_output=True, text=True, timeout=3
        ).stdout
        m = re.search(r"current (\d+) x (\d+)", out)
        if m:
            return int(m.group(1)), int(m.group(2))
    except (OSError, subprocess.SubprocessError):
        pass
    return None


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


VOLUME_BOOST_PERCENT = 150


def boost_audio_volume(
    app_name: str,
    percent: int = VOLUME_BOOST_PERCENT,
    retries: int = 12,
    interval: float = 0.5,
) -> None:
    """Raise the browser's per-app volume above 100% via pactl (some
    streams are quiet even at full volume). Must be redone after every
    load, and retried until the browser's sink input appears."""
    for _ in range(max(1, retries)):
        try:
            out = subprocess.run(
                ["pactl", "list", "sink-inputs"],
                capture_output=True, text=True, timeout=3,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return
        boosted = False
        for block in out.split("Sink Input #")[1:]:
            id_match = re.match(r"(\d+)", block)
            if id_match and f'application.name = "{app_name}"' in block:
                try:
                    subprocess.run(
                        ["pactl", "set-sink-input-volume", id_match.group(1), f"{percent}%"],
                        capture_output=True, timeout=3,
                    )
                    boosted = True
                except (OSError, subprocess.SubprocessError):
                    pass
        if boosted:
            return
        time.sleep(interval)


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
        boost_audio_volume("Google Chrome" if site_cfg.get("browser") == "chrome" else "LibreWolf")
    else:
        print("Attenzione: nessun tag <video> individuato entro 30s (potrebbe essere in un iframe o dietro un consenso cookie). Proseguo comunque.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--site",
        choices=sorted(SITES),
        default="mediaset",
        help="Sito da guardare (default: mediaset)",
    )
    parser.add_argument(
        "channel",
        nargs="?",
        default=None,
        help="Nome canale del sito scelto, oppure slug/URL diretto (default: primo canale del sito)",
    )
    parser.add_argument(
        "--profile-dir",
        default=None,
        help="Profilo LibreWolf persistente da usare (default: uno specifico per sito, mantiene DRM/Widevine, cookie e login tra un lancio e l'altro)",
    )
    parser.add_argument("--no-maximize", action="store_true", help="Non massimizzare la finestra all'avvio")
    parser.add_argument("--librewolf-path", default=None, help="Percorso del binario LibreWolf")
    parser.add_argument("--geckodriver-path", default=None, help="Percorso di geckodriver se non e' nel PATH")
    parser.add_argument("--chrome-path", default=None, help="Percorso del binario Google Chrome (siti con DRM/Widevine, es. la7)")
    parser.add_argument("--chromedriver-path", default=None, help="Percorso di chromedriver se non e' nel PATH (default: scaricato automaticamente da Selenium Manager)")
    parser.add_argument("--audio-device", default=None, help="Indice o nome del device di loopback da usare")
    parser.add_argument("--silence-threshold-db", type=float, default=-50.0, help="Soglia sotto la quale l'audio e' considerato silenzio (dBFS, default -50)")
    parser.add_argument("--silence-seconds", type=float, default=5.0, help="Silenzio continuo (secondi) prima di ricaricare la pagina (default 5)")
    parser.add_argument("--startup-grace-seconds", type=float, default=12.0, help="Tempo dopo ogni caricamento/reload in cui il silenzio non viene contato, per dare tempo allo stream di partire davvero (default 12)")
    parser.add_argument("--check-interval", type=float, default=0.5, help="Intervallo di campionamento audio in secondi (default 0.5)")
    parser.add_argument("--headless", action="store_true", help="Avvia LibreWolf in modalita' headless (sconsigliato: alcuni player si rifiutano di partire senza finestra visibile)")
    args = parser.parse_args()

    site_cfg = SITES[args.site]
    channels = site_cfg["channels"]
    channel = args.channel or next(iter(channels))

    slug = channels.get(channel, channel)
    if slug.startswith("http"):
        url = slug
    else:
        url = site_cfg["base_url"].format(slug=slug)

    browser = site_cfg.get("browser", "firefox")
    device = find_monitor_device(args.audio_device)
    device_info = sd.query_devices(device)
    samplerate = int(device_info["default_samplerate"])

    profile_dir = Path(args.profile_dir) if args.profile_dir else Path.home() / "TV" / site_cfg["profile_dirname"]
    profile_dir.mkdir(parents=True, exist_ok=True)

    if browser == "chrome":
        binary = find_chrome_binary(args.chrome_path)
        options = ChromeOptions()
        options.binary_location = binary
        options.add_argument(f"--user-data-dir={profile_dir}")
        # Allow autoplay with sound without any user gesture.
        options.add_argument("--autoplay-policy=no-user-gesture-required")
        options.add_argument("--no-first-run")
        if args.headless:
            options.add_argument("--headless=new")

        service_kwargs = {}
        if args.chromedriver_path:
            service_kwargs["executable_path"] = args.chromedriver_path
        service = ChromeService(**service_kwargs)

        print(f"Avvio Google Chrome ({binary}) su {url}")
        driver = webdriver.Chrome(options=options, service=service)
    else:
        binary = find_librewolf_binary(args.librewolf_path)
        options = FirefoxOptions()
        options.binary_location = binary
        options.add_argument("-profile")
        options.add_argument(str(profile_dir))
        # Allow autoplay with sound (0 = allow audio and video).
        options.set_preference("media.autoplay.default", 0)
        if args.headless:
            options.add_argument("-headless")

        service_kwargs = {}
        if args.geckodriver_path:
            service_kwargs["executable_path"] = args.geckodriver_path
        service = FirefoxService(**service_kwargs)

        print(f"Avvio LibreWolf ({binary}) su {url}")
        driver = webdriver.Firefox(options=options, service=service)
    if not args.headless and not args.no_maximize:
        # Maximized rather than fullscreen, so other windows (e.g. the
        # terminal) can still be brought to front.
        driver.maximize_window()

    try:
        load_page(driver, url, site_cfg)
        stream_ready_at = time.monotonic() + args.startup_grace_seconds

        print(f"Monitoraggio audio di sistema su device #{device} ({device_info['name']}, {samplerate} Hz)")
        print(f"Soglia silenzio: {args.silence_threshold_db} dBFS per {args.silence_seconds}s -> ricarico la pagina")

        silence_started = None
        block_size = max(1, int(samplerate * args.check_interval))
        screen_resolution = get_screen_resolution()
        fullscreen_state = is_fullscreen(driver, screen_resolution)
        next_fullscreen_check = time.monotonic() + 1.0
        system_muted = is_system_output_muted()
        next_mute_check = time.monotonic() + 1.0
        if system_muted:
            print("Attenzione: volume di sistema disattivato/a zero. Il controllo silenzio resta in pausa finche' non lo riattivi.")

        with sd.InputStream(device=device, channels=1, samplerate=samplerate, blocksize=block_size) as stream:
            while True:
                samples, _ = stream.read(block_size)
                level_db = rms_dbfs(samples[:, 0])
                now = time.monotonic()

                if now < stream_ready_at:
                    continue

                if now >= next_mute_check:
                    next_mute_check = now + 1.0
                    newly_muted = is_system_output_muted()
                    if newly_muted and not system_muted:
                        print(f"[{time.strftime('%H:%M:%S')}] Volume di sistema disattivato: metto in pausa il controllo silenzio finche' non lo riattivi.")
                    elif system_muted and not newly_muted:
                        print(f"[{time.strftime('%H:%M:%S')}] Volume di sistema riattivato, riprendo il controllo silenzio.")
                    system_muted = newly_muted

                if system_muted:
                    silence_started = None
                elif level_db < args.silence_threshold_db:
                    if silence_started is None:
                        silence_started = now
                    elapsed = now - silence_started
                    if elapsed >= args.silence_seconds:
                        print(f"[{time.strftime('%H:%M:%S')}] Silenzio da {elapsed:.0f}s ({level_db:.1f} dBFS) -> reload pagina")
                        was_fullscreen = is_fullscreen(driver, screen_resolution)
                        # Player fullscreen must be restored via the player,
                        # window fullscreen via fullscreen_window().
                        was_player_fullscreen = False
                        if was_fullscreen:
                            try:
                                was_player_fullscreen = bool(
                                    driver.execute_script("return !!document.fullscreenElement;")
                                )
                            except WebDriverException:
                                pass
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
                                boost_audio_volume("Google Chrome" if site_cfg.get("browser") == "chrome" else "LibreWolf")
                            if was_fullscreen:
                                if was_player_fullscreen and enter_player_fullscreen(driver):
                                    print("Schermo intero del player ripristinato.")
                                else:
                                    driver.fullscreen_window()
                                    print("Schermo intero (finestra) ripristinato.")
                                move_cursor_away(driver)
                                fullscreen_state = True
                                next_fullscreen_check = time.monotonic() + 1.0
                        except WebDriverException as exc:
                            print(f"Errore durante il reload: {exc}")
                        silence_started = None
                        stream_ready_at = time.monotonic() + args.startup_grace_seconds
                else:
                    silence_started = None

                # Also catch fullscreen entered manually; checked about once
                # per second to keep the audio loop light.
                if now >= next_fullscreen_check:
                    next_fullscreen_check = now + 1.0
                    currently_fullscreen = is_fullscreen(driver, screen_resolution)
                    if currently_fullscreen and not fullscreen_state:
                        move_cursor_away(driver)
                    fullscreen_state = currently_fullscreen
    except KeyboardInterrupt:
        print("\nInterrotto dall'utente.")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
