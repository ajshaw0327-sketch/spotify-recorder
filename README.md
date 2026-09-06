# Spotify Recorder for Raspberry Pi

This project records the Spotify audio that is playing **on a Raspberry Pi** and saves complete songs as tagged FLAC files. It is intended for a headless Pi: you control playback from the Spotify app, while the Pi runs the player and recorder over SSH.

Use it only for audio you are allowed to record. This program records the Pi's audio output; it does not download songs from YouTube and it does not bypass Spotify's access controls.

## What the program does

There are four pieces:

1. **Spotify app** — you choose music and choose the Pi as the playback device.
2. **Spotify Soloist** — Spotify's official headless Linux player. It plays music on the Pi and reports the current track locally.
3. **PipeWire/PulseAudio virtual sink** — a pretend audio output named `spotify_capture`. Soloist sends audio there instead of to speakers.
4. **`spotify_recorder.py`** — FFmpeg continuously records the sink to one FLAC file. When Soloist reports a complete, uninterrupted track, the script cuts that section into its own FLAC and adds metadata, artwork, and lyrics.

The phone is only the remote control. The Pi must be the Spotify playback device, otherwise there is no audio for the recorder to capture.

The recorder keeps the original continuous recording. This is deliberate: track-change events and audio samples do not arrive at exactly the same instant. If a split is slightly early or late, you can export the saved session again with a different offset without recording the music again.

## What you will need

- A Raspberry Pi running Raspberry Pi OS, connected to the network. These instructions use the account `pi` and hostname `raspberrypi.local`.
- A Spotify Premium account that can create a Soloist API key.
- A phone or computer with the Spotify app, on the same network as the Pi.
- Enough storage for the recordings. FLAC is lossless and much larger than MP3.
- [`spotify_recorder.py`](spotify_recorder.py) and [`requirements.txt`](requirements.txt).

## How to use this guide

Commands in a code block should be copied into an SSH terminal on the Pi unless the paragraph says **run this on your Mac**. Some steps use two SSH terminals:

- **Terminal 1** stays open running Soloist.
- **Terminal 2** runs the recorder and is where you press `Ctrl+C` when finished.

Do not run Soloist or the recorder with `sudo`. They need to use the same user's PipeWire audio session.

## 1. Connect to the Pi

On your Mac, open Terminal and connect:

```bash
ssh pi@raspberrypi.local
```

Enter the Pi user's password. A successful login gives you a prompt similar to `pi@raspberrypi:~ $`. If the hostname cannot be found, make sure the Pi is on the same network and that SSH is enabled. You can temporarily use the Pi's IP address instead.

## 2. Install system packages

Run these commands in the SSH session:

```bash
mkdir -p ~/spotify-recorder
cd ~/spotify-recorder
sudo apt update
sudo apt install -y ffmpeg python3 python3-venv python3-pip curl \
  pipewire pipewire-pulse pipewire-audio pulseaudio-utils
```

`ffmpeg` captures audio and writes FLAC. The PipeWire and PulseAudio packages provide the virtual output and the `pactl` diagnostic command.

On a minimal Raspberry Pi OS installation, reboot once after installing audio packages:

```bash
sudo reboot
```

Reconnect after the Pi comes back:

```bash
ssh pi@raspberrypi.local
cd ~/spotify-recorder
```

## 3. Copy the project files to the Pi

Run this **on your Mac**, from the folder containing the downloaded files:

```bash
scp spotify_recorder.py requirements.txt pi@raspberrypi.local:~/spotify-recorder/
```

If the files are in Downloads, use:

```bash
scp ~/Downloads/spotify_recorder.py ~/Downloads/requirements.txt \
  pi@raspberrypi.local:~/spotify-recorder/
```

Then, on the Pi, confirm both files arrived:

```bash
cd ~/spotify-recorder
ls -l spotify_recorder.py requirements.txt
```

If `scp` says `stat local ... No such file or directory`, the Mac filename or folder is wrong. Check it on the Mac with `ls -l ~/Downloads/`.

## 4. Create the Python environment

Run this on the Pi:

```bash
cd ~/spotify-recorder
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m py_compile spotify_recorder.py
```

The `(.venv)` at the beginning of your prompt means the environment is active. In every new SSH terminal, activate it again:

```bash
cd ~/spotify-recorder
. .venv/bin/activate
```

This version uses `requests`, `websocket-client`, and `mutagen`. It does not use Spotipy or `yt-dlp`: it records the audio Spotify is actually playing on the Pi.

## 5. Install Spotify Soloist

Soloist is a separate executable from this Python script. You need a Soloist API key from Spotify's developer dashboard. It is not the same as a Spotify Web API client secret. Follow Spotify's [Soloist getting-started guide](https://developer.spotify.com/documentation/soloist/tutorials/getting-started).

Check the Pi's architecture:

```bash
dpkg --print-architecture
```

For the 64-bit Raspberry Pi OS used here, the result is normally `arm64`, so download Spotify's Linux ARM64 archive. Copy it from your Mac (change the filename if necessary):

```bash
scp ~/Downloads/soloist_release_arm64.tar.gz \
  pi@raspberrypi.local:~/spotify-recorder/
```

On the Pi, extract and install it:

```bash
cd ~/spotify-recorder
tar -xzf soloist_release_arm64.tar.gz
mkdir -p ~/.local/bin
install -m 755 soloist ~/.local/bin/soloist
~/.local/bin/soloist --version
```

If the archive extracts into a subdirectory, locate the executable with `find ~/spotify-recorder -maxdepth 3 -type f -name soloist -ls`, then use that path in `install`. Soloist builds have a limited lifetime, so update the executable when it expires.

## 6. Create the audio capture output

The virtual sink is where Soloist sends audio. Its `monitor` source is what FFmpeg records. Run these commands on the Pi as user `pi`:

```bash
pactl info
pactl load-module module-null-sink \
  sink_name=spotify_capture rate=44100 channels=2
pactl list short sources
```

The last command must show `spotify_capture.monitor`. The numeric output from `pactl load-module` is a module ID; seeing a number means the sink was created. If you run the command twice, an "already exists" message is harmless when the monitor is already listed.

The sink usually disappears after a reboot or PipeWire restart. Re-run the load-module command whenever the monitor is missing.

## 7. Start Soloist (Terminal 1)

Open a **second** SSH window on your Mac:

```bash
ssh pi@raspberrypi.local
cd ~/spotify-recorder
```

Enter the Soloist key without printing it or placing it directly in shell history:

```bash
read -r -s -p "Soloist API key: " SOLOIST_API_KEY
printf '\n'
```

Paste the key and press Enter. Then start Soloist:

```bash
~/.local/bin/soloist \
  --device-name "Pi Recorder" \
  --api-key "$SOLOIST_API_KEY" \
  --pipewire-device spotify_capture \
  --ws 127.0.0.1:9090 \
  --cache-size 100
```

Leave this window open. Healthy startup includes messages like `websocket server listening`, `ready, device_name=Pi Recorder`, and `running`.

In Spotify on your phone or computer, choose **Pi Recorder** in the device picker. The phone is controlling the Pi; it is not the audio source itself.

## 8. Start recording (Terminal 2)

Use the original SSH window, or open another one. Activate the Python environment:

```bash
ssh pi@raspberrypi.local
cd ~/spotify-recorder
. .venv/bin/activate
```

Start with:

```bash
python spotify_recorder.py record \
  --source spotify_capture.monitor \
  --output ~/spotify-recordings
```

Wait for `Connected to Soloist metadata`, then press Play in Spotify. Start the first song from its beginning. For a first test, record one or two songs and press `Ctrl+C`.

### Recording one particular playlist

Find the playlist ID in its Spotify URL. In `https://open.spotify.com/playlist/4OD2YdOAQ61pqyod4rV3ti`, the ID is `4OD2YdOAQ61pqyod4rV3ti`.

```bash
python spotify_recorder.py record \
  --source spotify_capture.monitor \
  --output ~/spotify-recordings \
  --playlist 4OD2YdOAQ61pqyod4rV3ti
```

`--playlist` is a filter. It does not open the playlist, start it, download its track list, or fill in songs you did not play. You must start the playlist yourself in Spotify.

### Settings that improve results

Turn off Spotify **Crossfade**, **Automix**, **Repeat**, and **Autoplay** for clean individual files. Start songs at their beginning. Do not seek, pause for a long time, change speed, or move playback away from Pi Recorder. Crossfade mixes songs together and cannot be perfectly removed afterward.

### Stopping

Press `Ctrl+C` in the recorder terminal. It stops capture, preserves the session, and attempts to export complete tracks. Soloist can remain running for another session.

Stop after a fixed time:

```bash
python spotify_recorder.py record \
  --source spotify_capture.monitor \
  --output ~/spotify-recordings \
  --seconds 3600
```

Stop after Spotify becomes idle:

```bash
python spotify_recorder.py record \
  --source spotify_capture.monitor \
  --output ~/spotify-recordings \
  --stop-when-idle --idle-grace 30
```

`--stop-after-paused` may be used with `--stop-when-idle` when a long pause should end the run. Automatic idle stopping is optional because Spotify has no guaranteed “playlist finished” event.

## 9. Understand the output

The default output is `/home/pi/spotify-recordings`, also written as `~/spotify-recordings`:

```text
~/spotify-recordings/
├── metadata.json                 # local catalog of observed tracks
├── tracks/                       # finished, tagged FLAC files
└── sessions/SESSION_DIRECTORY/
    ├── session.flac              # continuous recording of the whole run
    ├── events.jsonl              # raw Soloist playback events
    ├── session.json               # detected segments and metadata
    ├── export-report.json         # saved/skipped/incomplete/failed counts
    └── capture.log                # FFmpeg diagnostics
```

The files are on the Pi's SD card. `tracks/` contains the files you normally want. Keep `sessions/` until you have checked the exports; it allows a retry without re-recording.

Messages mean:

- **`Ready to export: TITLE (track changed)`** — the previous play looked complete.
- **`Saved ARTIST - TITLE`** — a tagged FLAC was written to `tracks/`.
- **`Skipped existing ...`** — a valid export already exists, preventing duplicates.
- **`Kept in session only: TITLE (paused)`** — questionable audio remains in `session.flac` but was not split.
- **`buffering`** — Spotify temporarily stopped supplying audio. Short buffering is tolerated; prolonged buffering makes the segment incomplete.
- **`seek/restart or timing discontinuity`** — playback jumped, restarted, or moved devices.

An export report such as `{'saved': 2, 'skipped': 0, 'incomplete': 1, 'failed': 0}` means one section was deliberately not labeled a complete standalone song. Its audio may still be in `session.flac`.

## 10. Metadata, artwork, and lyrics

Each exported FLAC can contain title, artist, album, album artist, release date/year, track/disc number, genre, label, ISRC, Spotify links, and embedded album art. The exporter prefers the largest artwork cover provided by Soloist and can recover a larger cover from `events.jsonl` during a retry.

Lyrics are looked up from LRCLIB using title, artist, album, and duration. Time-synced lyrics are written to the FLAC `LYRICS` comment; unsynced lyrics are written to `UNSYNCEDLYRICS`. Missing lyrics or a lyric network failure does not stop export. Disable lookup with `--lyrics off`:

```bash
python spotify_recorder.py record \
  --source spotify_capture.monitor \
  --output ~/spotify-recordings \
  --lyrics off
```

A music player must support FLAC Vorbis comments and its own lyrics display. `LYRICS` is timestamped text data; the recorder does not alter the audio to make timing more accurate.

## 11. Re-export a saved session

Use the session path printed by the recorder:

```bash
cd ~/spotify-recorder
. .venv/bin/activate
python spotify_recorder.py export \
  ~/spotify-recordings/sessions/20260906T193709Z-973a09b3
```

Valid files already in `tracks/` are skipped. If splits start slightly late, adjust the offset and replace old files:

```bash
python spotify_recorder.py export \
  ~/spotify-recordings/sessions/20260906T193709Z-973a09b3 \
  --offset-ms 250 --replace
```

Positive `--offset-ms` moves split positions later. It cannot repair a song that was paused, buffered too long, or started halfway through.

## 12. Copy finished files to your Mac

Run this on the **Mac**, not inside SSH:

```bash
mkdir -p ~/Downloads/spotify-tracks
scp -r pi@raspberrypi.local:~/spotify-recordings/tracks/. \
  ~/Downloads/spotify-tracks/
```

The FLAC files are now in `~/Downloads/spotify-tracks`. To copy sessions for backup or later export:

```bash
scp -r pi@raspberrypi.local:~/spotify-recordings/sessions \
  ~/Downloads/spotify-recording-sessions
```

## 13. Optional Spotify Web API enrichment

Soloist already provides the metadata needed for normal export. Optional Spotify Web API credentials can add release date, album artist, track/disc numbers, ISRC, label, and genre.

Create a Spotify developer application, then make this protected file on the Pi:

```bash
mkdir -p ~/.config
cat > ~/.config/spotify-api.env <<'EOF'
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
EOF
chmod 600 ~/.config/spotify-api.env
```

For a one-off recording, load it into the current terminal before starting the recorder:

```bash
set -a
. ~/.config/spotify-api.env
set +a
```

The client-credentials flow is only for metadata. Recording continues with Soloist metadata if the Web API is unavailable. Never put secrets in a public repository or screenshot.

## 14. Troubleshooting

### `spotify_capture.monitor` is missing

Run `pactl info` and `pactl list short sources` as user `pi`. If the monitor is absent, recreate it:

```bash
pactl load-module module-null-sink \
  sink_name=spotify_capture rate=44100 channels=2
```

### Soloist starts but the recorder cannot connect

Confirm Soloist is still running in Terminal 1 and is listening on `127.0.0.1:9090`. Confirm the recorder uses the same WebSocket address and that both processes run as `pi`.

### Files are silent

Check that Soloist used `--pipewire-device spotify_capture`, Spotify is playing to **Pi Recorder**, and the source is exactly `spotify_capture.monitor`. Read `capture.log` inside the session for FFmpeg errors.

### Playback buffers or a track is incomplete

The Pi did not receive uninterrupted audio. Improve Wi-Fi/Ethernet and replay the track from its beginning. A retry cannot recreate audio that never arrived.

### Album art is small or missing

Keep `events.jsonl` and re-export after the script has seen the larger cover:

```bash
python spotify_recorder.py export \
  ~/spotify-recordings/sessions/SESSION_DIRECTORY --replace
```

### I interrupted a song

The final section remains in `session.flac` but is normally marked incomplete. Start a new session and replay it from the beginning.

### I started two recorders

Stop one. The script locks the output directory so a second recorder/exporter should exit rather than corrupting the same session.

## 15. Keeping it running after SSH disconnects

The simplest method is to keep both SSH windows open. Closing an SSH window normally stops foreground programs. If you need unattended operation, use `tmux` or create user-level systemd services after the manual setup works. Do not put the Soloist key directly into a public service file.

On a headless Pi, user lingering can help start user services at boot:

```bash
sudo loginctl enable-linger pi
systemctl --user enable --now pipewire.socket pipewire-pulse.socket wireplumber.service
```

You still need to recreate the `spotify_capture` null sink after boot unless you configure it as a permanent PipeWire/PulseAudio node.

## Limitations

- Recording happens in real time. The program cannot create a song that was not played.
- Track boundaries are approximate because playback events and captured samples have different clocks.
- Crossfade, seeking, pausing, device changes, and long buffering can make a segment incomplete.
- `--playlist` filters observed playback context; it is not a playlist downloader.
- Lyrics and artwork depend on external network services and may be unavailable.
- The continuous session is retained and can consume substantial storage.

## Development check

Syntax-check the script without starting Spotify:

```bash
python -m py_compile spotify_recorder.py
```

The Raspberry Pi audio path and Soloist pairing require a live Pi test. Successful track IDs are skipped on later exports unless `--replace` is supplied.

