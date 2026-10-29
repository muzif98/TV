# TV

Watch Italian live TV channels from a single shell command.

Each command opens the broadcaster's official live page in a browser,
dismisses the cookie banner (rejecting non-essential cookies), presses Play
where needed and makes sure audio is on. It then watches the system audio
output: if the stream goes silent for a few seconds (a frozen or stalled
stream), it reloads the page automatically and restores fullscreen if you
were watching in fullscreen.

```sh
git clone https://github.com/muzif98/TV.git ~/TV && cd ~/TV
./setup.sh
source ~/.zshrc
rai1
```

## Contents

- [How it works](#how-it-works)
- [Channels](#channels)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [Windows version](#windows-version)
- [Troubleshooting](#troubleshooting)
- [Project structure](#project-structure)
- [Limitations and legal note](#limitations-and-legal-note)
- [License](#license)

## How it works

1. `silence-refresh.py` starts a browser through Selenium with a dedicated,
   persistent profile per site (cookies and settings survive between runs).
2. It opens the channel's live page, handles the site's cookie banner and,
   on sites that need it, clicks the player's Play button.
3. Once a `<video>` element appears it unmutes it and raises the browser's
   per-application volume to 150% via `pactl` (some streams are quiet).
4. It reads the system audio loopback (the PulseAudio/PipeWire monitor of
   the default output) and computes the level every 0.5 s.
5. If the level stays below -50 dBFS for 5 s, the page is reloaded and the
   same steps repeat; fullscreen is restored and the mouse pointer is moved
   out of the way. Silence is ignored for 12 s after each (re)load, and
   while the system output is muted or at zero volume.

Audio is measured at system level rather than inside the page because
browsers do not let page scripts read audio from DRM-protected media.
See [DESIGN.md](DESIGN.md) for the reasoning behind these choices.

Browsers used: **LibreWolf** for most sites, **Google Chrome** for La7,
whose stream requires the Widevine CDM that LibreWolf does not ship.

## Channels

| Site (`--site`) | Channel commands |
|---|---|
| Mediaset Infinity (`mediaset`) | canale5 · italia1 · italia2 · rete4 · canale20 · iris · la5 · cine34 · focus · topcrime · mediasetextra · tgcom24 |
| RaiPlay (`rai`) | rai1 · rai2 · rai3 · rai4 · rai5 · raigulp · raimovie · rainews24 · raipremium · rairadio2 · raiscuola · raisport · raistoria · raiyoyo |
| La7 (`la7`) | la7 |
| TV8 (`tv8`) | tv8 |
| Nove (`nove`) | nove |
| Warner Bros. Discovery (`discovery`) | dmax · realtime · giallo · motortrend · foodnetwork |

34 channels in total. The shell command `canale20` maps to the script's
channel name `20mediaset`; every other command has the same name as the
channel.

## Requirements

- Debian or a Debian-based distribution (the setup script uses `apt`).
- A running graphical desktop (X11 or Wayland): real browser windows are
  opened.
- A working audio server: PipeWire (with `pipewire-pulse`) or PulseAudio,
  with `pactl` available.
- `git`, `sudo` rights for package installation.
- `xrandr` (optional): used to detect window fullscreen.

Installed by `setup.sh` if missing: `curl`, `ca-certificates`,
`python3-venv`, `zsh`, `portaudio19-dev`, LibreWolf (from its official
`extrepo` repository), `geckodriver` (latest Mozilla release),
Google Chrome (only needed for La7), and a Python virtualenv with
`selenium`, `sounddevice` and `numpy`.

## Installation

```sh
git clone https://github.com/muzif98/TV.git ~/TV
cd ~/TV
./setup.sh
```

`setup.sh` is idempotent: it can be run again and skips what is already
installed. It:

1. installs the missing system packages (asks for your `sudo` password);
2. creates the virtualenv in `.venv/` inside the project directory;
3. appends one shell function per channel to `~/.zshrc`, inside a
   `# >>> Live TV ... <<< Live TV <<<` block. If the block is already
   present (detected through the `la7` function) `~/.zshrc` is left alone.

Then open a new terminal or run `source ~/.zshrc`. If your login shell is
not zsh, the script tells you how to switch (`chsh -s $(command -v zsh)`).

## Usage

With the shell functions:

```sh
rai1
canale5 --silence-seconds 8
```

Directly:

```sh
.venv/bin/python silence-refresh.py --site <site> [channel]
.venv/bin/python silence-refresh.py --site rai rai1
```

Without `--site` the `mediaset` site is used; without a channel, the first
channel of the site. The channel argument can also be a raw slug or a full
URL. Press `Ctrl+C` to stop; the browser is closed.

The browser window starts maximized (not fullscreen). Use the player's
fullscreen button or F11 as usual: after an automatic reload the same kind
of fullscreen is restored.

## Configuration

All settings are command-line options (`--help` for the full list; help
texts are in Italian):

| Option | Default | Meaning |
|---|---|---|
| `--site` | `mediaset` | Site to open |
| `--silence-threshold-db` | `-50` | Level (dBFS) below which audio counts as silence |
| `--silence-seconds` | `5` | Continuous silence before reloading |
| `--startup-grace-seconds` | `12` | Time after each (re)load during which silence is ignored |
| `--check-interval` | `0.5` | Audio sampling interval in seconds |
| `--audio-device` | auto | Index or name of the loopback input device |
| `--profile-dir` | `~/TV/<profile>` | Persistent browser profile directory |
| `--no-maximize` | off | Do not maximize the window at start |
| `--headless` | off | Headless browser (not recommended: some players refuse to start) |
| `--librewolf-path`, `--geckodriver-path` | auto | Paths to LibreWolf and geckodriver |
| `--chrome-path`, `--chromedriver-path` | auto | Paths to Chrome and chromedriver (chromedriver is fetched by Selenium Manager if absent) |

Extra options passed to a channel command are forwarded to the script.

Browser profiles are stored by default in `~/TV/` (e.g.
`~/TV/.librewolf-rai-profile`), regardless of where the project is cloned;
cloning into `~/TV` keeps everything in one place (the profile folders are
git-ignored). The per-app volume boost (150%) is a constant
(`VOLUME_BOOST_PERCENT`) in `silence-refresh.py`.

## Windows version

The `windows/` folder contains `tv_windows.py`, a Windows port with the
same core behaviour but:

- Google Chrome only;
- a small graphical window (tkinter) instead of shell functions: pick a
  site and a channel, press **Guarda**;
- audio captured through WASAPI loopback, mute detection through `pycaw`;
- no Discovery channels, no per-app volume boost, and fullscreen is
  restored as window fullscreen only.

Google Chrome must be installed. To run from source:
`pip install -r windows/requirements.txt` and `python windows/tv_windows.py`.
To build a standalone `TV.exe`, on Windows:

```
cd windows
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --noconsole --name TV tv_windows.py
```

> The Windows version has not been tested on real Windows hardware; WASAPI loopback capture and mute detection may need
> adjustments.

## Troubleshooting

- **"Nessun device di loopback trovato"**: no monitor device was found.
  List the devices with
  `.venv/bin/python -c "import sounddevice as sd; print(sd.query_devices())"`
  and pass the right one with `--audio-device`.
- **"Impossibile caricare PortAudio"**: install `portaudio19-dev`.
- **The page reloads too often**: quiet passages may fall below the
  threshold, or the stream may need longer to start. Try
  `--silence-threshold-db -60` or larger `--silence-seconds` /
  `--startup-grace-seconds` values.
- **Reloads never happen while another app plays sound**: the whole system
  output is monitored, so keep other audio sources quiet.
- **Nove / Discovery channels stay stuck loading**: turn off any VPN; these
  streams appear to be geo-restricted.
- **A channel no longer opens**: the broadcaster may have changed the URL.
  Open the live page in a normal browser and update the slug in the
  `SITES` table of `silence-refresh.py`.
- **Cookie banner not dismissed**: the site changed its banner; dismiss it
  once by hand, the choice is kept in the persistent profile.
- **La7 does not play**: make sure Google Chrome is installed (`la7` uses
  Chrome for Widevine).
- **No sound / too quiet**: check that the system output is not muted;
  the script pauses silence detection and prints a warning when it is.

## Project structure

```
.
├── silence-refresh.py               Linux script: browser automation + silence watchdog
├── setup.sh                         Linux installer (packages, venv, zsh functions)
├── windows/
│   ├── tv_windows.py                Windows port with a tkinter GUI
│   └── requirements.txt             Python dependencies for the Windows build
├── DESIGN.md                        Design decisions and rationale
└── README.md
```

Program messages and `--help` texts are in Italian.

## Limitations and legal note

- This project is not affiliated with, endorsed by or connected to any of
  the broadcasters mentioned. Channel and brand names belong to their
  owners.
- It only automates a regular browser on the broadcasters' own official,
  freely accessible live web pages: it opens the page, clicks the cookie
  banner and Play button, and reloads the page. It does not download,
  record, redistribute or decrypt any stream; protected streams are
  played by the browser's own DRM module (Widevine in Chrome) as on a
  normal visit. Using these sites remains subject to their terms of use
  and to geographic availability.
- Site layouts change: channel URLs and cookie-banner selectors may need
  updating over time.
- Silence detection uses the whole system output, not just the browser.
- Linux support targets Debian-based systems with PulseAudio/PipeWire.

## License

MIT. See [LICENSE](LICENSE).
