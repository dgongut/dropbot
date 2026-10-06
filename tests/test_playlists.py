"""Playlists: qué hacer al recibir una y cuántos vídeos bajar como mucho."""

import asyncio

import pytest

import settings


@pytest.fixture
def launched(quiet_bot, monkeypatch, config_dir):
    """Lo que se le pasaría a run_url_download, sin lanzar yt-dlp."""
    calls = []

    def fake_run(event, cmd, status_message, final_output_dir, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return asyncio.sleep(0)

    monkeypatch.setattr(quiet_bot, "run_url_download", fake_run)
    return quiet_bot, calls


def _start(dropbot, make_event, run_async, playlist_count, mode):
    run_async(dropbot.start_ytdlp_download(make_event(), None, "https://x/list", False, playlist_count, mode))


class TestTope:
    def test_sin_tope_baja_la_playlist_entera(self, launched, make_event, run_async):
        dropbot, calls = launched

        _start(dropbot, make_event, run_async, 300, "full")

        assert "--playlist-end" not in calls[0]["cmd"]
        assert calls[0]["total_videos"] == 300

    def test_con_tope_corta_la_playlist(self, launched, make_event, run_async):
        dropbot, calls = launched
        settings.put("urls.playlist_limit", 50)

        _start(dropbot, make_event, run_async, 300, "full")

        cmd = calls[0]["cmd"]
        assert cmd[cmd.index("--playlist-end") + 1] == "50"
        # El progreso cuenta sobre lo que de verdad se va a bajar
        assert calls[0]["total_videos"] == 50

    def test_un_tope_mayor_que_la_playlist_no_cambia_la_cuenta(self, launched, make_event, run_async):
        dropbot, calls = launched
        settings.put("urls.playlist_limit", 100)

        _start(dropbot, make_event, run_async, 7, "full")

        assert calls[0]["total_videos"] == 7

    def test_solo_el_primero_no_usa_el_tope(self, launched, make_event, run_async):
        dropbot, calls = launched
        settings.put("urls.playlist_limit", 50)

        _start(dropbot, make_event, run_async, 300, "first")

        assert "--no-playlist" in calls[0]["cmd"]
        assert "--playlist-end" not in calls[0]["cmd"]
        assert calls[0]["total_videos"] == 1


class TestModo:
    @pytest.fixture
    def playlist_link(self, launched, monkeypatch):
        dropbot, calls = launched

        async def not_direct(url):
            return False, None, None, None, None

        async def is_playlist(url):
            return True, 12, "Lista"

        monkeypatch.setattr(dropbot, "is_direct_download_url", not_direct)
        monkeypatch.setattr(dropbot, "detect_playlist", is_playlist)
        return dropbot, calls

    def _send_link(self, dropbot, make_event, run_async):
        event = make_event(event_id=77)
        event.raw_text = "https://x/list"
        run_async(dropbot.handle_url_link(event))

    def test_preguntar_ofrece_entera_o_el_primero(self, playlist_link, sent_messages, make_event, run_async):
        dropbot, calls = playlist_link

        self._send_link(dropbot, make_event, run_async)

        assert not calls
        assert "77" in dropbot.pending_urls

    @pytest.mark.parametrize("mode,flag", [("FULL", "--ignore-errors"), ("FIRST", "--no-playlist")])
    def test_un_modo_fijo_no_pregunta(self, playlist_link, make_event, run_async, mode, flag):
        dropbot, calls = playlist_link
        settings.put("urls.playlist", mode)
        settings.put("urls.auto_format", "VIDEO")

        self._send_link(dropbot, make_event, run_async)

        assert len(calls) == 1
        assert flag in calls[0]["cmd"]
