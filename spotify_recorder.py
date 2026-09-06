#!/usr/bin/env python3
"""Record the Pi's Spotify audio output; split/tag uninterrupted tracks afterward.

Spotify Soloist supplies local metadata over WebSocket. FFmpeg captures a named
PulseAudio/PipeWire monitor continuously. Optional Spotify Web API credentials
enrich exported tags with release and album data. Timing is approximate; keep
the session original.
See README.md for setup, audio routing, and limitations.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid

import requests
import websocket
from mutagen import MutagenError
from mutagen.flac import FLAC, Picture

LOG = logging.getLogger("spotify-recorder")


class SpotifyEnricher:
    """Optionally add canonical album/release fields from Spotify's Web API."""

    def __init__(self, http):
        self.http = http
        self.client_id = os.getenv("SPOTIFY_CLIENT_ID")
        self.client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        self.token = None
        self.album_cache = {}
        self.warned = False

    def _access_token(self):
        if self.token:
            return self.token
        if not self.client_id or not self.client_secret:
            if not self.warned:
                LOG.info("Spotify Web API credentials not set; using Soloist metadata only")
                self.warned = True
            return None
        response = self.http.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret), timeout=15)
        response.raise_for_status()
        self.token = response.json()["access_token"]
        return self.token

    def enrich(self, track):
        token = self._access_token()
        if not token:
            return track
        try:
            headers = {"Authorization": f"Bearer {token}"}
            response = self.http.get(
                f"https://api.spotify.com/v1/tracks/{track['id']}",
                headers=headers, timeout=15)
            if response.status_code == 401:
                self.token = None
                token = self._access_token()
                headers = {"Authorization": f"Bearer {token}"}
                response = self.http.get(
                    f"https://api.spotify.com/v1/tracks/{track['id']}",
                    headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()
            album = data.get("album") or {}
            artists = album.get("artists") or []
            enriched = dict(track)
            if album.get("name"):
                enriched["album"] = album["name"]
            if artists:
                enriched["album_artist"] = ", ".join(a.get("name", "") for a in artists if a.get("name"))
            for source, destination in (("track_number", "track_number"), ("disc_number", "disc_number")):
                if data.get(source) is not None:
                    enriched[destination] = str(data[source])
            release_date = album.get("release_date")
            if release_date:
                enriched["release_date"] = release_date
                enriched["release_year"] = str(release_date)[:4]
            external_ids = data.get("external_ids") or {}
            if external_ids.get("isrc"):
                enriched["isrc"] = external_ids["isrc"]
            album_id = album.get("id")
            if album_id:
                album_data = self.album_cache.get(album_id)
                if album_data is None:
                    album_response = self.http.get(
                        f"https://api.spotify.com/v1/albums/{album_id}",
                        headers=headers, timeout=15)
                    album_response.raise_for_status()
                    album_data = album_response.json()
                    self.album_cache[album_id] = album_data
                genres = album_data.get("genres") or []
                if genres:
                    enriched["genre"] = ", ".join(genres)
                if album_data.get("label"):
                    enriched["label"] = album_data["label"]
            return enriched
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            LOG.warning("Spotify Web API enrichment failed for %s: %s", track.get("title"), exc)
            return track


def save_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


@contextmanager
def locked(path):
    with Path(path).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another recorder/export is using this directory") from exc
        yield


def metadata(item):
    if not isinstance(item, dict):
        return None
    uri = item.get("uri", "")
    if not re.fullmatch(r"spotify:track:[A-Za-z0-9]{22}", uri):
        return None  # Skip advertisements, episodes, and unavailable entries.
    decorations = item.get("decorations") or {}
    title = (decorations.get("identity") or {}).get("name")
    album_entity = (decorations.get("parent") or {}).get("entity") or {}
    album = ((album_entity.get("decorations") or {}).get("identity") or {}).get("name", "")
    artists = []
    for creator in decorations.get("creators") or []:
        entity = creator.get("entity") or {}
        name = ((entity.get("decorations") or {}).get("identity") or {}).get("name")
        if name:
            artists.append(name)
    duration = (decorations.get("playback") or {}).get("duration_ms")
    artwork_url = best_cover_url(decorations)
    if not title or not artists or not isinstance(duration, (int, float)) or not 0 < duration < 86400000:
        return None
    # Soloist's decoration shape has changed between releases. Keep the
    # common fields when present, but tolerate missing optional fields.
    album_artist = first_scalar(decorations, {"album_artist", "albumArtist"})
    release_date = first_scalar(decorations, {"release_date", "releaseDate", "date"})
    track_number = first_scalar(decorations, {"track_number", "trackNumber"})
    disc_number = first_scalar(decorations, {"disc_number", "discNumber"})
    isrc = first_scalar(decorations, {"isrc", "ISRC"})
    genre = first_scalar(decorations, {"genre", "genres"})
    label = first_scalar(decorations, {"label", "record_label", "recordLabel"})
    return {"id": uri.split(":")[-1], "uri": uri, "title": title,
            "artists": artists, "artist": ", ".join(artists), "album": album,
            "album_artist": album_artist or ", ".join(artists),
            "release_date": release_date, "release_year": str(release_date)[:4] if release_date else None,
            "track_number": track_number, "disc_number": disc_number, "isrc": isrc,
            "genre": genre, "label": label, "duration_ms": duration,
            "artwork_url": artwork_url}


def first_scalar(value, keys):
    """Find the first useful scalar for a field in nested Soloist metadata."""
    if isinstance(value, dict):
        for key, candidate in value.items():
            if key in keys and isinstance(candidate, (str, int, float)):
                return str(candidate)
            found = first_scalar(candidate, keys)
            if found:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = first_scalar(candidate, keys)
            if found:
                return found
    return None


def best_cover_url(decorations):
    covers = (decorations.get("visual_identity") or {}).get("cover") or []
    rank = {"small": 0, "default": 1, "large": 2, "xlarge": 3}
    usable = [c for c in covers if c.get("url")]
    if not usable:
        return None
    return max(usable, key=lambda c: rank.get(str(c.get("size", "default")).lower(), 1)).get("url")


def artwork_overrides(events_path):
    """Recover the largest cover seen in the event log for an older session."""
    overrides = {}
    try:
        with Path(events_path).open(encoding="utf-8") as events:
            for line in events:
                try:
                    event = json.loads(line).get("event") or {}
                    for item in (event.get("item"),):
                        if not isinstance(item, dict):
                            continue
                        track = metadata(item)
                        if track and track.get("artwork_url"):
                            overrides[track["id"]] = track["artwork_url"]
                except (TypeError, ValueError):
                    continue
    except FileNotFoundError:
        pass
    return overrides


class Tracker:
    """Conservative track intervals derived from local playback-state observations.

    An interval interrupted by pause, seek, prolonged buffering, or lost metadata is excluded.
    Complete means an uninterrupted, duration-consistent observation, not proof
    of sample-perfect boundaries or of the identity of the captured audio.
    """
    def __init__(self, playlist=None):
        self.playlist = f"spotify:playlist:{playlist}" if playlist else None
        self.current = None
        self.segments = []
        self.catalog = {}
        self.earliest = 0.0
        self.buffering_since = None

    def close(self, elapsed, reason, natural=False):
        run = self.current
        earliest = elapsed
        if run:
            end = run["start"] + run["track"]["duration_ms"] / 1000
            complete = (natural and run["samples"] >= 2
                        and run["start"] >= max(0, run["earliest"] - 0.25)
                        and abs(elapsed - end) <= 3
                        and elapsed >= end - 0.25
                        and elapsed - run["last_seen"] <= 5)
            self.segments.append({"track": run["track"], "start": run["start"],
                                  "end": end, "observed_until": elapsed,
                                  "complete": complete, "reason": reason})
            if natural and abs(elapsed - end) <= 3:
                earliest = min(elapsed, end)
            LOG.info("%s: %s (%s)", "Ready to export" if complete else "Kept in session only",
                     run["track"]["title"], reason)
        self.current = None
        self.earliest = earliest
        self.buffering_since = None

    def observe(self, state, elapsed, wall_time):
        track = metadata(state.get("item"))
        context = state.get("context") or {}
        context_uri = context.get("uri") if isinstance(context, dict) else context
        status = state.get("status")
        if status == "buffering" and self.current:
            if self.buffering_since is None:
                self.buffering_since = elapsed
            elif elapsed - self.buffering_since > 10:
                self.close(elapsed, "buffering longer than 10 seconds")
            return
        if status == "playing":
            self.buffering_since = None
        if (not track or status != "playing" or not state.get("is_active")
                or (self.playlist and context_uri != self.playlist)):
            self.close(elapsed, "not playing the requested music", natural=state.get("status") == "idle")
            return
        self.catalog[track["id"]] = track
        position = state.get("position") or {}
        progress, stamp, speed = (position.get(k) for k in ("position_ms", "timestamp_ms", "speed"))
        if (not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (progress, stamp, speed))
                or progress < 0 or abs(speed - 1) > 0.01):
            self.close(elapsed, "missing/invalid playback timing")
            return
        age = wall_time - stamp / 1000
        # This is an anchor timestamp, not response freshness: it can remain
        # unchanged throughout a long track. Fresh observations track liveness.
        if age < -1:
            self.close(elapsed, "system clock mismatch")
            return
        progress = (progress / 1000) + max(0, age) * speed
        if progress > track["duration_ms"] / 1000 + 5:
            self.close(elapsed, "playback position exceeds track duration")
            return
        anchor = elapsed - progress
        if self.current and self.current["track"]["id"] != track["id"]:
            self.close(elapsed, "next track", natural=True)
        if self.current:
            if elapsed - self.current["last_seen"] > 5:
                self.close(elapsed, "metadata gap")
            elif abs(anchor - self.current["start"]) > 0.8:
                self.close(elapsed, "seek/restart or timing discontinuity")
        if self.current is None:
            self.current = {"track": track, "start": anchor, "last_seen": elapsed,
                            "samples": 1, "earliest": self.earliest}
        else:
            self.current["samples"] += 1
            self.current["last_seen"] = elapsed
            self.current["track"] = track


def stop_capture(process):
    if process.poll() is None:
        try:
            process.communicate(input=b"q\n", timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return process.returncode


def record(args):
    root = args.output.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    with locked(root / ".record.lock"):
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        session = root / "sessions" / name
        session.mkdir(parents=True)
        tracker = Tracker(args.playlist)
        manifest = {"schema": 1, "output_root": str(root), "source": args.source,
                    "playlist": args.playlist, "audio": "session.flac", "segments": []}
        save_json(session / "session.json", manifest)
        command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-n",
                   "-f", "pulse", "-sample_rate", "44100", "-channels", "2",
                   "-fragment_size", "4096", "-i", args.source,
                   "-map", "0:a:0", "-c:a", "flac", "-compression_level", "0",
                   "-threads", "1", str(session / "session.flac")]
        sock = None
        catalog = load_json(root / "metadata.json", {})
        if not isinstance(catalog, dict):
            raise RuntimeError("metadata.json must contain an object")
        with (session / "capture.log").open("wb") as capture_log, (session / "events.jsonl").open("a", encoding="utf-8") as events:
            start = time.monotonic()
            manifest["started_at"] = time.time()
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                       stderr=capture_log, start_new_session=True)
            next_request, next_connect, backoff, saved_segments = 0.0, 0.0, 1.0, 0
            seen_playing, idle_since, paused_since = False, None, None
            last_persist = start
            LOG.info("Recording to %s. Start playlist playback now; Ctrl+C stops and exports.", session)
            try:
                while process.poll() is None:
                    now = time.monotonic()
                    elapsed = now - start
                    if args.seconds and elapsed >= args.seconds:
                        break
                    if sock is None:
                        if now < next_connect:
                            time.sleep(0.1)
                            continue
                        try:
                            sock = websocket.create_connection(args.websocket, timeout=2)
                            sock.settimeout(0.25)
                            next_request = 0.0
                            LOG.info("Connected to Soloist metadata")
                        except (OSError, websocket.WebSocketException) as exc:
                            tracker.close(elapsed, "metadata disconnected")
                            LOG.warning("Metadata unavailable: %s; capture continues", exc)
                            next_connect, backoff = now + backoff, min(30, backoff * 2)
                            continue
                    try:
                        if now >= next_request:
                            sock.send(json.dumps({"type": "command", "command": "get_state"}))
                            next_request = now + 2
                        message = sock.recv()
                        if not message:
                            raise websocket.WebSocketConnectionClosedException("connection closed")
                        event = json.loads(message)
                        if not isinstance(event, dict):
                            continue
                        backoff = 1.0
                        elapsed, wall = time.monotonic() - start, time.time()
                        events.write(json.dumps({"elapsed": elapsed, "wall_time": wall, "event": event}, ensure_ascii=False) + "\n")
                        kind = event.get("type")
                        if kind == "playback_state":
                            status = event.get("status")
                            if status == "playing":
                                seen_playing, idle_since, paused_since = True, None, None
                            elif status == "idle" and args.stop_when_idle and seen_playing:
                                idle_since = idle_since or elapsed
                            elif status == "paused" and args.stop_after_paused and seen_playing:
                                paused_since = paused_since or elapsed
                            tracker.observe(event, elapsed, wall)
                        elif kind == "track_changed":
                            item = event.get("item") or {}
                            if tracker.current and item.get("uri") != tracker.current["track"]["uri"]:
                                tracker.close(elapsed, "track changed", natural=True)
                            next_request = 0.0
                        elif kind == "playback_changed":
                            status = event.get("status")
                            if status == "playing":
                                seen_playing, idle_since, paused_since = True, None, None
                            elif status == "idle" and args.stop_when_idle and seen_playing:
                                idle_since = idle_since or elapsed
                            elif status == "paused" and args.stop_after_paused and seen_playing:
                                paused_since = paused_since or elapsed
                            if status == "buffering" and tracker.current:
                                if tracker.buffering_since is None:
                                    tracker.buffering_since = elapsed
                            elif status != "playing":
                                tracker.close(elapsed, str(status), natural=status == "idle")
                            else:
                                tracker.buffering_since = None
                            next_request = 0.0
                        elif kind == "device_changed" and not event.get("is_active"):
                            tracker.close(elapsed, "playback moved away from this device")
                        elif kind == "position_sync":
                            # Anchor changes include seeks and restarts. Treat an
                            # unexplained jump as an incomplete segment before the
                            # next full state can accidentally mark it natural.
                            position = event.get("position") or {}
                            progress = position.get("position_ms")
                            stamp = position.get("timestamp_ms")
                            speed = position.get("speed")
                            if tracker.current and all(isinstance(v, (int, float)) and math.isfinite(v)
                                                       for v in (progress, stamp, speed)):
                                estimated_start = elapsed - progress / 1000 - max(0, wall - stamp / 1000) * speed
                                if speed == 0 and tracker.buffering_since is not None:
                                    pass
                                elif speed <= 0 or abs(estimated_start - tracker.current["start"]) > 1.0:
                                    tracker.close(elapsed, "position anchor changed (seek/restart)")
                            next_request = 0.0
                        if (args.stop_when_idle and seen_playing and idle_since is not None
                                and elapsed - idle_since >= args.idle_grace):
                            LOG.info("Soloist has been idle for %.0fs; stopping automatically", args.idle_grace)
                            break
                        if (args.stop_after_paused and seen_playing and paused_since is not None
                                and elapsed - paused_since >= args.idle_grace):
                            LOG.info("Soloist has been paused for %.0fs; stopping automatically", args.idle_grace)
                            break
                    except websocket.WebSocketTimeoutException:
                        pass
                    except (OSError, websocket.WebSocketException, ValueError) as exc:
                        tracker.close(time.monotonic() - start, "metadata connection/error")
                        LOG.warning("Metadata interrupted: %s; capture continues", exc)
                        if sock:
                            sock.close()
                        sock, next_connect = None, time.monotonic() + backoff
                        backoff = min(30, backoff * 2)
                    if len(tracker.segments) != saved_segments or time.monotonic() - last_persist >= 5:
                        catalog.update(tracker.catalog)
                        if catalog != load_json(root / "metadata.json", {}):
                            save_json(root / "metadata.json", catalog)
                        manifest["segments"] = tracker.segments
                        save_json(session / "session.json", manifest)
                        events.flush()
                        os.fsync(events.fileno())
                        saved_segments, last_persist = len(tracker.segments), time.monotonic()
            except KeyboardInterrupt:
                LOG.info("Stopping recording")
            finally:
                tracker.close(time.monotonic() - start, "recording stopped", natural=True)
                if sock:
                    sock.close()
                status = stop_capture(process)
                manifest.update(segments=tracker.segments, capture_returncode=status, stopped_at=time.time())
                save_json(session / "session.json", manifest)
                catalog.update(tracker.catalog)
                save_json(root / "metadata.json", catalog)
        LOG.info("Session retained: %s", session)
        if status != 0:
            raise RuntimeError(f"FFmpeg capture failed (exit {status}); inspect {session / 'capture.log'}")
        return export(session, offset_ms=0, lyrics_source=args.lyrics)


def get_artwork(track, root, http):
    url = track.get("artwork_url")
    if not url:
        return None
    directory = root / ".artwork"
    directory.mkdir(exist_ok=True)
    path = directory / (hashlib.sha256(url.encode()).hexdigest() + ".img")
    if path.exists():
        data = path.read_bytes()
    else:
        for attempt in range(3):
            try:
                with http.get(url, timeout=(10, 25), stream=True) as response:
                    response.raise_for_status()
                    data = bytearray()
                    for chunk in response.iter_content(65536):
                        data.extend(chunk)
                        if len(data) > 5 * 1024 * 1024:
                            raise RuntimeError("Artwork exceeds 5 MiB limit")
                data = bytes(data)
                break
            except requests.RequestException as exc:
                code = exc.response.status_code if exc.response is not None else None
                if attempt == 2 or (code is not None and code != 429 and code < 500):
                    raise
                delay = 2 ** (attempt + 1)
                if code == 429:
                    try:
                        delay = max(delay, float(exc.response.headers.get("Retry-After", 0)))
                    except ValueError:
                        pass
                LOG.warning("Artwork download interrupted; retrying in %.1fs", delay)
                time.sleep(delay)
    mime = "image/jpeg" if data.startswith(b"\xff\xd8\xff") else "image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else None
    if not mime:
        raise RuntimeError("Unsupported/missing artwork image")
    if not path.exists():
        fd, temporary = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return mime, data


def get_lyrics(track, http, source):
    """Return LRCLIB's synced/plain lyrics, or None when unavailable.

    Lyrics are a separate best-effort metadata request; a missing result never
    discards an otherwise valid recording.
    """
    if source == "off":
        return None
    params = {"track_name": track["title"], "artist_name": track["artist"],
              "album_name": track.get("album", ""),
              "duration": int(round(track["duration_ms"] / 1000))}
    try:
        response = http.get("https://lrclib.net/api/get", params=params,
                            headers={"User-Agent": "spotify-recorder/1.0"}, timeout=(10, 20))
        if response.status_code == 404:
            LOG.info("No lyrics found for %s", track["title"])
            return None
        response.raise_for_status()
        payload = response.json()
        synced = payload.get("syncedLyrics")
        plain = payload.get("plainLyrics")
        if not isinstance(synced, str):
            synced = None
        if not isinstance(plain, str):
            plain = None
        if not synced and not plain:
            return None
        return {"synced": synced, "plain": plain, "source": "LRCLIB"}
    except (requests.RequestException, ValueError) as exc:
        LOG.warning("Lyrics lookup failed for %s; exporting without lyrics: %s", track["title"], exc)
        return None


def valid_final(path, track):
    try:
        audio = FLAC(path)
        tags = audio.tags or {}
        return bool(tags.get("spotify_id") == [track["id"]]
                    and abs(audio.info.length - track["duration_ms"] / 1000) < 0.5
                    and all(tags.get(tag) for tag in ("title", "artist", "album"))
                    and (not track.get("artwork_url") or audio.pictures))
    except (OSError, MutagenError, ValueError):
        return False


def export(session, offset_ms=0, replace=False, lyrics_source="lrclib"):
    session = Path(session).expanduser().resolve()
    manifest = load_json(session / "session.json")
    if not manifest or manifest.get("schema") != 1:
        raise RuntimeError("Missing or incompatible session.json")
    root = Path(manifest["output_root"])
    audio_path = session / manifest["audio"]
    output = root / "tracks"
    output.mkdir(parents=True, exist_ok=True)
    with locked(root / ".export.lock"), requests.Session() as http:
        spotify = SpotifyEnricher(http)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(audio_path)],
                               capture_output=True, text=True, check=True, timeout=30)
        recorded_duration = float(json.loads(probe.stdout)["format"]["duration"])
        counts = {"saved": 0, "skipped": 0, "incomplete": 0, "failed": 0}
        failures = []
        overrides = artwork_overrides(session / "events.jsonl")
        for segment in manifest["segments"]:
            if not segment["complete"]:
                counts["incomplete"] += 1
                continue
            track = segment["track"]
            track = spotify.enrich(track)
            if overrides.get(track["id"]):
                track = dict(track, artwork_url=overrides[track["id"]])
            final = output / f"{track['id']}.flac"
            if not replace and valid_final(final, track):
                counts["skipped"] += 1
                continue
            start = segment["start"] + offset_ms / 1000
            duration = track["duration_ms"] / 1000
            if start < 0 or start + duration > recorded_duration:
                counts["incomplete"] += 1
                continue
            temporary = output / f".{track['id']}.pending.flac"
            try:
                cover = get_artwork(track, root, http)
                lyrics = get_lyrics(track, http, lyrics_source)
                command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                           "-threads", "1", "-ss", f"{start:.6f}", "-i", str(audio_path),
                           "-t", f"{duration:.6f}", "-map", "0:a:0", "-vn", "-map_metadata", "-1",
                           "-c:a", "flac", "-compression_level", "5", "-threads", "1", str(temporary)]
                result = subprocess.run(command, capture_output=True, text=True, timeout=max(120, duration * 3))
                if result.returncode:
                    raise RuntimeError(f"FFmpeg export failed: {result.stderr[-1000:]}")
                tagged = FLAC(temporary)
                if tagged.tags is None:
                    tagged.add_tags()
                tags = tagged.tags
                tags["TITLE"] = [track["title"]]
                tags["ARTIST"] = [track["artist"]]
                tags["ALBUM"] = [track["album"]]
                optional_tags = {
                    "ALBUMARTIST": track.get("album_artist"),
                    "DATE": track.get("release_date"),
                    "YEAR": track.get("release_year"),
                    "TRACKNUMBER": track.get("track_number"),
                    "DISCNUMBER": track.get("disc_number"),
                    "GENRE": track.get("genre"),
                    "LABEL": track.get("label"),
                    "ISRC": track.get("isrc"),
                }
                for tag_name, tag_value in optional_tags.items():
                    if tag_value:
                        tags[tag_name] = [str(tag_value)]
                tags["SPOTIFY_ID"] = [track["id"]]
                tags["RECORDING_SESSION"] = [session.name]
                tags["SPLIT_ACCURACY"] = ["Approximate playback-event timing; verify boundaries"]
                tags["SPOTIFY_URL"] = [f"https://open.spotify.com/track/{track['id']}"]
                tags["SPOTIFY_URI"] = [track["uri"]]
                if lyrics:
                    if lyrics.get("synced"):
                        tags["LYRICS"] = [lyrics["synced"]]
                    if lyrics.get("plain"):
                        tags["UNSYNCEDLYRICS"] = [lyrics["plain"]]
                    tags["LYRICS_SOURCE"] = [lyrics["source"]]
                if cover:
                    picture = Picture()
                    picture.type = 3
                    picture.mime = cover[0]
                    picture.desc = "Cover"
                    picture.data = cover[1]
                    tagged.clear_pictures()
                    tagged.add_picture(picture)
                tagged.save()
                if not valid_final(temporary, track):
                    raise RuntimeError("Audio duration or tag verification failed")
                with temporary.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.replace(temporary, final)
                counts["saved"] += 1
                LOG.info("Saved %s - %s", track["artist"], track["title"])
            except (OSError, RuntimeError, ValueError, requests.RequestException, MutagenError, subprocess.TimeoutExpired) as exc:
                counts["failed"] += 1
                failures.append({"id": track["id"], "error": str(exc)})
                LOG.error("Export failed for %s: %s", track["title"], exc)
            finally:
                temporary.unlink(missing_ok=True)
        save_json(session / "export-report.json", {"counts": counts, "failures": failures,
                                                    "offset_ms": offset_ms, "lyrics_source": lyrics_source})
        LOG.info("Export complete: %s; continuous recording retained", counts)
        return 2 if counts["failed"] or counts["incomplete"] or not (counts["saved"] + counts["skipped"]) else 0


def stop_signal(signum, frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("record", help="Capture continuously, then export on stop")
    capture.add_argument("--source", required=True, help="Explicit monitor source, e.g. spotify_capture.monitor")
    capture.add_argument("--output", type=Path, default=Path("recordings"))
    capture.add_argument("--websocket", default="ws://127.0.0.1:9090")
    capture.add_argument("--playlist", help="Optional playlist ID; only export playback from this context")
    capture.add_argument("--seconds", type=float, help="Stop automatically after this many seconds")
    capture.add_argument("--lyrics", choices=["lrclib", "off"], default="lrclib",
                         help="Lyrics metadata source (default: LRCLIB; audio still exports if unavailable)")
    capture.add_argument("--stop-when-idle", action="store_true",
                         help="Stop and export after playback has started and Soloist stays idle")
    capture.add_argument("--stop-after-paused", action="store_true",
                         help="Also stop after a long pause (use with --stop-when-idle)")
    capture.add_argument("--idle-grace", type=float, default=15,
                         help="Seconds Soloist must remain idle before automatic stop (default: 15)")
    split = commands.add_parser("export", help="Retry exports from a saved session without recording again")
    split.add_argument("session", type=Path)
    split.add_argument("--offset-ms", type=float, default=0, help="Shift split positions later by this many milliseconds")
    split.add_argument("--replace", action="store_true", help="Re-export and atomically replace existing tracks, e.g. after adjusting offset")
    split.add_argument("--lyrics", choices=["lrclib", "off"], default="lrclib",
                       help="Lyrics metadata source (default: LRCLIB; use off to skip lookup)")
    args = parser.parse_args()
    if args.command == "record":
        if args.playlist and not re.fullmatch(r"[A-Za-z0-9]{22}", args.playlist):
            parser.error("--playlist requires a 22-character Spotify playlist ID")
        if args.seconds is not None and (not math.isfinite(args.seconds) or args.seconds <= 0):
            parser.error("--seconds must be positive and finite")
        if not math.isfinite(args.idle_grace) or args.idle_grace < 1:
            parser.error("--idle-grace must be at least 1 second")
        if args.stop_after_paused and not args.stop_when_idle:
            parser.error("--stop-after-paused requires --stop-when-idle")
    elif not math.isfinite(args.offset_ms):
        parser.error("--offset-ms must be finite")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return record(args) if args.command == "record" else export(args.session, args.offset_ms, args.replace, args.lyrics)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop_signal)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        LOG.warning("Interrupted; session files are retained")
        sys.exit(130)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        LOG.error("%s", exc)
        sys.exit(1)
