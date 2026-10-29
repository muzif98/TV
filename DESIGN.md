# Design notes

This document explains the main design decisions behind `silence-refresh.py`
(Linux) and `windows/tv_windows.py` (Windows), and why they were made.

## Goal

Live web streams of Italian channels occasionally freeze: the picture
stops and the audio goes silent, and the player does not recover on its
own. Reloading the page fixes it. The tool automates "open the channel,
click through the cookie banner and Play, and reload whenever it freezes",
so a channel can be left running unattended.

## Detecting a frozen stream through system audio

The signal used is *silence*: a working live channel almost never stays
silent for several seconds, while a frozen one does.

The obvious approach would be to attach a Web Audio `AnalyserNode` to the
page's `<video>` element. That does not work reliably: some streams are
protected with EME/Widevine, and browsers deliberately prevent page
scripts from extracting audio from protected media, so an analyser inside
the page would read silence even on a perfectly working stream.

Reading the system loopback instead (the *monitor* source of the
PulseAudio/PipeWire default sink on Linux, a WASAPI loopback of the default
output device on Windows) captures audio *after* decoding, so it works
regardless of DRM and regardless of which player the site uses.

The trade-off is that the whole system output is measured: any other sound
playing at the same time masks a frozen stream. This is documented as a
limitation.

### PipeWire specifics

On PulseAudio, PortAudio exposes a "Monitor of ..." input device that can be
opened directly. On PipeWire systems it usually does not; instead there is
a generic ALSA device called `pulse`. The script sets
`PULSE_SOURCE=@DEFAULT_SINK@.monitor` in its environment *before* opening the
stream, so that the PulseAudio compatibility layer connects the `pulse`
device to the monitor of the current default output. Device lookup first
tries a "monitor" device, then falls back to `pulse`, and finally lets the
user choose with `--audio-device`.

### Thresholds and timing

- The level is computed as RMS in dBFS on blocks of `--check-interval`
  seconds (0.5 s).
- Below `-50 dBFS` counts as silence; 5 s of continuous silence triggers a
  reload. These defaults are conservative enough to ignore short pauses
  but react quickly to a frozen stream.
- After every load or reload, silence is ignored for 12 s
  (`--startup-grace-seconds`), because players need time to buffer and
  start; without this the tool could reload a page that is simply still
  loading, in a loop.

### Muted system output

If the user mutes the speakers or sets the volume to 0, the monitor reads
silence and the tool would reload forever. The script therefore checks the
default sink's mute/volume state about once per second (`pactl` on Linux,
`pycaw` / Core Audio on Windows) and pauses silence detection, printing a
message, until the output is unmuted.

## Browser choice

- **LibreWolf** is used for most sites: a privacy-focused Firefox build,
  driven through geckodriver. It is installed as a native package rather
  than a Flatpak, because the Flatpak does not expose a binary geckodriver
  can launch.
- **Google Chrome** is used for La7, whose stream is protected by
  Widevine. LibreWolf removes the Widevine CDM by policy, so it cannot
  play that stream; Chrome ships it. The browser is chosen per site via the
  `browser` key in the `SITES` table.
- On Windows only Chrome is used: it covers every site, and a single
  browser keeps the packaged executable simple.

### Persistent profiles

Each site gets its own persistent browser profile (`~/TV/.librewolf-<site>-profile`,
`~/TV/.chrome-la7-profile`; `%LOCALAPPDATA%\TV\chrome-<site>-profile` on
Windows). This keeps cookie choices, the downloaded DRM module and any
login between runs, and isolates sites from each other. The profile
folders are git-ignored.

### Autoplay

Players are often blocked or forced to start muted by autoplay policies.
The tool addresses this at several levels:

- LibreWolf: preference `media.autoplay.default = 0` (allow audio and
  video), so it does not need to be enabled by hand in every new profile.
- Chrome: `--autoplay-policy=no-user-gesture-required`. With it the La7
  stream starts with sound without clicking Play.
- After the `<video>` element appears, a script sets `muted = false` and
  `volume = 1.0` on every video element. This is done on every site,
  because some players start muted intermittently (especially in LibreWolf,
  where muted autoplay can be allowed when audible autoplay is not).

### Volume boost

`HTMLMediaElement.volume` is capped at 1.0, and some streams (TV8 in
particular) are quiet even with the system mixer at 100%. On Linux the
script raises the browser's per-application volume (its sink inputs) to
150% with `pactl set-sink-input-volume`; PipeWire allows values above 100%.

The boost has to be reapplied after every load and reload, because the
browser recreates its audio stream and the per-stream volume is not
restored. On first launch the browser's sink input may not exist yet when
the boost is attempted (browser and audio pipeline still warming up), so
the function retries every 0.5 s, up to 12 times, until at least one
stream of the application has been adjusted. Sink inputs are matched by
`application.name` ("LibreWolf" or "Google Chrome").

## Site-specific automation

Each site entry in `SITES` holds a base URL, the channel-to-slug map, the
profile directory name, and two optional hooks: `accept_cookies` and
`click_play`. Adding a site means adding an entry and, if needed, two small
functions. Channel slugs were copied from each site's live page; if a
broadcaster changes them, updating the table is enough. When the slug
starts with `http` it is used as the full URL: the Discovery channels each
live on a different domain, so their "slug" is the whole URL and the base
URL is just `{slug}`.

Cookie banners are always answered with the option that rejects
non-essential cookies:

- **Mediaset Infinity**: open the detailed panel, then "necessary only".
- **RaiPlay**: the banner's "continue without accepting" button.
- **TV8**: the consent banner lives in a cross-origin iframe
  (`cmp.sky.it`); the driver switches into the iframe, clicks the reject
  choice, and always switches back to the main document.
- **Nove / Discovery**: OneTrust banner. The preferences button is
  `position: fixed`; a native Selenium click failed with "could not be
  scrolled into view" because the driver's scroll-into-view logic does not
  handle fixed elements well, so it is clicked via JavaScript. Then
  "essential cookies only" is chosen.
- **La7**: the first-level reject button is labelled "REJECT & CONTINUE"
  or "Continua senza accettare" depending on the language the consent
  platform detects, so it is matched case-insensitively on either
  "reject" or "senza accettare". The "Preferences" panel is never opened:
  clicking into it reliably hung the automation indefinitely.

Play buttons:

- **RaiPlay**: the `<video>` element does not exist until the play button
  is pressed, so it is clicked before waiting for the video.
- **Nove / Discovery**: the Video.js big play button must be clicked; the
  stream then starts muted and is unmuted by the generic unmute step.
- **La7**: the homepage only shows a muted decorative preview; the real
  player is on `/dirette-tv`, and Chrome's autoplay flag starts it.

All hooks are tolerant: if an element does not show up within its timeout
(for example because the cookie choice was already stored in the profile),
the step is skipped silently. If no `<video>` appears within 30 s the tool
warns and continues monitoring anyway.

## Fullscreen handling

### Window maximized, not fullscreen, at start

The window starts maximized rather than in exclusive fullscreen. On
Wayland a true fullscreen window cannot be covered by anything, not even
the terminal running the script; a maximized window stays a normal window
that can be switched away from with a click or Alt+Tab.

### Detecting fullscreen

There are two kinds of fullscreen:

- **player fullscreen**, entered with the player's own button (Fullscreen
  API): detected with `document.fullscreenElement`;
- **window fullscreen** (F11 / window manager): detected by comparing the
  browser's outer size with the physical screen resolution. A maximized
  window is slightly smaller because of panels and decorations.

The screen resolution is read with `xrandr` on Linux (`GetSystemMetrics` on
Windows, after enabling DPI awareness so scaling does not skew it). JavaScript's
`window.screen` is not usable for this on Firefox under Wayland: its values
follow the window size (they change with a plain maximize), so they cannot
serve as a reference.

The state is polled about once per second, not on every audio block, to
keep JavaScript round-trips out of the sampling loop.

### Restoring fullscreen after a reload

Before reloading, the script records whether the browser was in
fullscreen and whether that was player fullscreen. After the reload:

- `driver.fullscreen_window()` only makes the *window* fullscreen; the
  video stays at its in-page size, which from the user's point of view
  means "fullscreen did not come back". So player fullscreen must be
  restored through the player.
- `element.requestFullscreen()` cannot be called from `execute_script`:
  browsers reject it without a real user gesture. Selenium input actions,
  however, do count as user gestures.

`enter_player_fullscreen()` therefore hovers the video (to reveal the
auto-hidden controls), clicks the player's fullscreen button (Video.js or
JW Player selectors), or, if none is found, double-clicks the video, which
toggles fullscreen in most players. It then verifies that
`document.fullscreenElement` is set and returns `True` only in that case;
otherwise the caller falls back to window fullscreen. This keeps the logic
independent of the specific player each site uses.

The Windows version currently only restores window fullscreen.

### Moving the pointer

Whenever fullscreen is entered, manually or after a reload, the mouse
pointer is moved to the top-left corner so it does not sit over the video.
W3C actions measure `move_to_element_with_offset` offsets from the
element's centre, not its corner, so the offset from the centre of the
`<body>` to (1, 1) is computed from the viewport size.

## Installation script

`setup.sh` is meant to be run on a fresh Debian desktop and re-run safely:
each step checks whether its component is already present. It installs
only what cannot be assumed (it does not install or replace the audio
server, to avoid overriding the one in use). LibreWolf comes from its
official repository via `extrepo`, geckodriver from Mozilla's latest
GitHub release. Channels are exposed as zsh functions rather than aliases
so extra options can be forwarded with `"$@"`.

## Windows port

`windows/tv_windows.py` reuses the same site table and DOM automation (they
do not depend on browser or OS) and replaces the Linux-specific parts:
WASAPI loopback instead of the PulseAudio monitor, `pycaw` instead of
`pactl`, `GetSystemMetrics` instead of `xrandr`. A tkinter GUI (bundled with
Python, so no extra UI dependency) replaces the shell functions, because
a packaged `.exe` is the natural way to launch it on Windows. Monitoring
runs in a background thread with a stop event, and log lines are forwarded
to the GUI since the packaged executable has no console. The executable is
built with PyInstaller (`--onefile --noconsole`). If no
loopback device is available, the channel still opens, just without
automatic reloads.
