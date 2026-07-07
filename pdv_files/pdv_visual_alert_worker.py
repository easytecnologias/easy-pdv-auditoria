#!/usr/bin/env python3
"""Worker de saúde e vídeos do PDV.

Responsabilidades:
- Ping de health a cada 30s
- Upload de vídeos de cupons para o dashboard
- Poll de requests de vídeo pendentes

A auditoria visual é feita pelo pdv-groq-auditor.service (groq_auditor.py).
"""
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/pdv-telegram-assistant")

import pdv_telegram_assistant as bot
try:
    import dashboard_reporter as _dr
except ImportError:
    _dr = None
import threading as _threading
import queue as _queue

_VIDEO_STATE = '/var/lib/pdv-visual-auditor/cupom_videos.txt'
POLL_SECONDS = float(os.environ.get("VISUAL_ALERT_POLL_SECONDS", "30"))


def log(message):
    print("%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def _load_video_set():
    try:
        return set(open(_VIDEO_STATE).read().splitlines())
    except Exception:
        return set()


def _mark_video_done(num):
    try:
        open(_VIDEO_STATE, 'a').write(num + '\n')
    except Exception:
        pass


uploaded_cupom_videos = _load_video_set()
_video_queue = _queue.Queue()


def _video_queue_worker():
    while True:
        _item = _video_queue.get()
        try:
            if _dr:
                if _item.get('_is_request'):
                    _dr.processar_video_request(_item)
                else:
                    _dr.postar_video_cupom(_item)
                    _mark_video_done(str(_item.get('number', '')))
        except Exception as exc:
            log("erro video queue: %s: %s" % (type(exc).__name__, exc))
        finally:
            _video_queue.task_done()


_threading.Thread(target=_video_queue_worker, daemon=True).start()


def _video_fast_poll():
    """Poll de requests de vídeo a cada 8s."""
    while True:
        time.sleep(8)
        try:
            if _dr:
                for _req in (_dr.poll_video_pendentes() or []):
                    _video_queue.put(dict(_req, _is_request=True))
        except Exception as exc:
            log("erro poll video pendente: %s: %s" % (type(exc).__name__, exc))


_threading.Thread(target=_video_fast_poll, daemon=True).start()


def _handle_shutdown_signal(signum, frame):
    raise SystemExit(0)


def clean_bot_args():
    sys.argv = [sys.argv[0]]


def main():
    clean_bot_args()
    args = bot.parse_args()
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    log("worker iniciado (health + sales + videos)")
    last_health_ping = 0.0

    try:
        while True:
            try:
                _now_mono = time.monotonic()

                if _dr and _now_mono - last_health_ping >= 30:
                    _dr.postar_health(True)
                    try:
                        cups, _, _ = bot.read_sales(args)
                        import datetime as _dt
                        bot.set_query_date(args, _dt.datetime.today())
                        _dr.postar_vendas(cups)
                        for _cup in cups:
                            _cnum = str(_cup.get('number', ''))
                            if _cup.get('closed') and _cnum and _cnum not in uploaded_cupom_videos:
                                uploaded_cupom_videos.add(_cnum)
                                _video_queue.put(_cup)
                        for _req in (_dr.poll_video_pendentes() or []):
                            _video_queue.put(dict(_req, _is_request=True))
                    except Exception as _se:
                        log('erro vendas/videos: %s' % _se)
                    last_health_ping = time.monotonic()

                time.sleep(POLL_SECONDS)
            except Exception as exc:
                log("erro: %s: %s" % (type(exc).__name__, exc))
                time.sleep(5)
    except SystemExit:
        raise


if __name__ == "__main__":
    main()
