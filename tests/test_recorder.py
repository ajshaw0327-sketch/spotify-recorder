"""Offline regression tests. Run: python -m unittest discover -s tests -v"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location('recorder', Path(__file__).resolve().parents[1] / 'spotify_recorder.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def ffmpeg(*args):
    return subprocess.run(['ffmpeg', '-v', 'error', '-y', *map(str, args)], check=True,
                          capture_output=True, timeout=60)


def track(letter='A', seconds=2):
    return dict(id=letter*22, uri='spotify:track:'+letter*22, title='Test',
                artist='Test artist', album='Test album', artwork_url=None,
                duration_ms=seconds*1000)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = self.root / 'sessions' / 'test'
        (self.session / 'chunks').mkdir(parents=True)
        self.metadata = patch.object(r.SpotifyEnricher, 'enrich', lambda _, t: t)
        self.metadata.start()
        self.addCleanup(self.metadata.stop)
        self.lyrics = patch.object(r, 'get_lyrics', return_value={'synced': '[00:00.00]Example', 'plain': 'Example', 'source': 'test'})
        self.lyrics.start()
        self.addCleanup(self.lyrics.stop)

    def manifest(self, start, t=None, schema=2):
        m = dict(schema=schema, output_root=str(self.root), audio='chunks.csv' if schema == 2 else 'session.flac',
                 segments=[dict(start=start, complete=True, track=t or track())])
        r.save_json(self.session/'session.json', m)
        return m

    def generate(self, seconds=8, chunk_seconds=2):
        source = self.session/'original.flac'
        ffmpeg('-f','lavfi','-i','anoisesrc=sample_rate=44100:seed=42', '-t',seconds,
               '-ac','2','-sample_fmt','s16','-c:a','flac',source)
        ffmpeg('-i',source,'-af','asettb=1/44100,asetpts=N','-c:a','flac','-compression_level','0','-threads','1',*r.chunk_output_args(self.session, chunk_seconds))
        return source

    def decode(self, path, start=0, seconds=2):
        return ffmpeg('-i',path,'-ss',start,'-t',seconds,'-f','s16le','-c:a','pcm_s16le','pipe:1').stdout

    def test_cross_chunk_sample_exact_and_duplicate_skip(self):
        original = self.generate()
        self.manifest(1.75)
        self.assertEqual(r.export(self.session), 0)
        final = self.root/'tracks'/('A'*22+'.flac')
        self.assertEqual(self.decode(final), self.decode(original, 1.75))
        self.assertEqual(r.FLAC(final)['LYRICS'], ['[00:00.00]Example'])
        self.assertEqual(r.export(self.session), 0)
        self.assertEqual(r.load_json(self.session/'export-report.json')['counts']['skipped'], 1)
        self.assertEqual(r.export(self.session, replace=True), 0)

    def test_hour_long_capture_does_not_read_earlier_chunks(self):
        # One real hour of generated audio, encoded faster than real time.
        ffmpeg('-f','lavfi','-i','anullsrc=r=44100:cl=stereo','-t','3600',
               '-c:a','flac','-threads','1',*r.chunk_output_args(self.session))
        index = r.ChunkIndex(self.session).refresh()
        rows = index.interval(3571, 2)
        self.assertLessEqual(sum(end-begin for _,begin,end in rows), 31)
        self.assertGreater(rows[0][1], 3500)
        # Remove every earlier audio chunk: late export must not open any.
        needed = {row[0] for row in rows}
        for path in (self.session/'chunks').glob('*.flac'):
            if path.name not in needed:
                path.unlink()
        m = self.manifest(3571)
        started = time.monotonic()
        self.assertEqual(r.export(self.session, live_manifest=m), 0)
        print(f'One-hour late-song export: {time.monotonic()-started:.2f}s; {len(rows)} chunk(s)')

    def test_live_closed_chunks_and_unfinished_tail(self):
        m = self.manifest(0.25)
        capture = subprocess.Popen(['ffmpeg','-v','error','-re','-f','lavfi','-i',
                                    'sine=sample_rate=44100','-t','30','-c:a','flac',
                                    *r.chunk_output_args(self.session,2)], stdin=subprocess.PIPE)
        try:
            deadline = time.monotonic()+12
            while time.monotonic()<deadline:
                index = r.ChunkIndex(self.session).refresh()
                if index.interval(0.25, 2):
                    break
                time.sleep(.2)
            self.assertIsNone(capture.poll())
            self.assertEqual(r.export(self.session, live_manifest=m), 0)
            self.assertIsNone(capture.poll())
            future = self.manifest(25, track('B'))
            self.assertEqual(r.export(self.session, live_manifest=future), 2)
            self.assertFalse((self.root/'tracks'/('B'*22+'.flac')).exists())
        finally:
            r.stop_capture(capture)
        self.manifest(.25)
        self.assertEqual(r.export(self.session), 0)

    def test_missing_chunk_rejects_file_but_exports_next_song(self):
        self.generate()
        (self.session/'chunks'/'chunk-000000001.flac').unlink()
        m = self.manifest(1.75)
        m['segments'].append(dict(start=5, complete=True, track=track('B')))
        r.save_json(self.session/'session.json',m)
        self.assertEqual(r.export(self.session), 2)
        self.assertFalse((self.root/'tracks'/('A'*22+'.flac')).exists())
        self.assertTrue((self.root/'tracks'/('B'*22+'.flac')).exists())
        self.assertFalse(list((self.root/'tracks').glob('.*.pending.flac')))

    def test_partial_csv_line_and_gap(self):
        path = self.session/'chunks.csv'
        path.write_text('chunk-000000000.flac,0,2\nchunk-000000001.flac,2,')
        index = r.ChunkIndex(self.session).refresh()
        self.assertEqual(len(index.rows),1)
        with path.open('a') as out:
            out.write('4\nchunk-000000002.flac,6,8\n')
        index.refresh()
        self.assertEqual(len(index.rows),3)
        self.assertFalse(index.interval(3,4))
        self.assertTrue(index.interval(6,1))

    def test_legacy_export_still_works(self):
        source = self.generate()
        source.rename(self.session/'session.flac')
        self.manifest(2,schema=1)
        self.assertEqual(r.export(self.session),0)

    def test_export_lock_preserves_files(self):
        self.generate()
        self.manifest(0)
        with r.locked(self.root/'.export.lock'):
            with self.assertRaises(RuntimeError):
                r.export(self.session)
        self.assertFalse(list((self.root/'tracks').glob('*.flac')))


class NetworkTests(unittest.TestCase):
    def test_token_failure_is_optional(self):
        http = Mock()
        http.post.side_effect = r.requests.ConnectionError('offline')
        with patch.dict(os.environ, SPOTIFY_CLIENT_ID='test', SPOTIFY_CLIENT_SECRET='test'):
            self.assertEqual(r.SpotifyEnricher(http).enrich(track()),track())

    def test_lyrics_503_is_optional(self):
        http = Mock()
        http.get.return_value.raise_for_status.side_effect = r.requests.HTTPError('503')
        http.get.return_value.status_code = 503
        self.assertIsNone(r.get_lyrics(track(),http,'lrclib'))


class LifecycleTests(unittest.TestCase):
    def test_capture_exports_during_metadata_outage_then_finalizes(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = SimpleNamespace(output=root, playlist=None, source='test', websocket='test',
                                   lyrics='off', seconds=6, no_live_export=False, min_free_mb=64,
                                   stop_when_idle=False, stop_after_paused=False, idle_grace=15)
            real_popen = subprocess.Popen
            real_chunks = r.chunk_output_args
            real_export = r.export
            captures, live_results, stopped_before_worker_return = [], [], []
            tracker = r.Tracker(None)
            # Completed metadata was observed before the simulated outage.
            tracker.segments = [dict(complete=True,start=0.2,track=track())]

            def popen(command, *pos, **kw):
                if 'pulse' in command:
                    first = command.index('-map')
                    command = ['ffmpeg','-v','error','-re','-f','lavfi','-i',
                               'sine=sample_rate=44100','-t','30',*command[first:]]
                    process = real_popen(command,*pos,**kw)
                    captures.append(process)
                    return process
                return real_popen(command,*pos,**kw)

            def export(*pos, **kw):
                result = real_export(*pos, **kw)
                if kw.get('live_manifest'):
                    live_results.append((result,captures[0].poll()))
                    # Hold the one active worker until capture reaches its stop deadline.
                    time.sleep(3)
                    stopped_before_worker_return.append(bool(r.load_json(Path(pos[0])/'session.json').get('stopped_at')))
                return result

            with patch.object(r.subprocess,'Popen',side_effect=popen), \
                 patch.object(r,'chunk_output_args',side_effect=lambda session:real_chunks(session,2)), \
                 patch.object(r,'Tracker',return_value=tracker), \
                 patch.object(r.websocket,'create_connection',side_effect=r.websocket.WebSocketException('offline')), \
                 patch.object(r.SpotifyEnricher,'enrich',lambda _,t:t), \
                 patch.object(r,'export',side_effect=export):
                self.assertEqual(r.record(args),0)
            self.assertEqual(live_results,[(0,None)])
            self.assertEqual(stopped_before_worker_return,[True])
            self.assertIsNotNone(captures[0].poll())
            session = next((root/'sessions').iterdir())
            self.assertTrue(r.load_json(session/'session.json')['stopped_at'])
            self.assertEqual(r.load_json(session/'export-report.json')['counts']['skipped'],1)

    def test_low_space_refuses_capture_without_launching_ffmpeg(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(output=Path(tmp),playlist=None,source='test',min_free_mb=512)
            with patch.object(r.shutil,'disk_usage',return_value=SimpleNamespace(free=1)), \
                 patch.object(r.subprocess,'Popen') as spawn:
                with self.assertRaisesRegex(RuntimeError,'Insufficient free disk'):
                    r.record(args)
                spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
