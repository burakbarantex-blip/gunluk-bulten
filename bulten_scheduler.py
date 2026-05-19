# -*- coding: utf-8 -*-
"""
Günlük Bülten Scheduler v2.0
Railway'de sürekli çalışan worker — Her gün 09:30 TR saatinde bülteni tetikler.
"""

import time, logging, subprocess, sys
from datetime import datetime, timedelta, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("scheduler")

TZ_TR          = timezone(timedelta(hours=3))
TARGET_HOUR    = 9
TARGET_MINUTE  = 30


def next_run_time():
    now    = datetime.now(TZ_TR)
    target = now.replace(hour=TARGET_HOUR, minute=TARGET_MINUTE, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return target


def run_bulten():
    # Hafta sonu kontrolü (0=Pazartesi, 6=Pazar)
    bugun = datetime.now(TZ_TR).weekday()
    if bugun >= 5:  # Cumartesi=5, Pazar=6
        log.info(f"🗓️ Hafta sonu — bülten gönderilmedi.")
        return

    log.info("📧 Bülten v2.0 çalıştırılıyor...")
    try:
        result = subprocess.run(
            [sys.executable, "bulten.py"],
            capture_output=True, text=True, timeout=600  # Claude API için 10 dk
        )
        if result.returncode == 0:
            log.info("✅ Bülten başarıyla gönderildi!")
        else:
            log.error(f"❌ Bülten hatası (exit={result.returncode})")
            log.error(f"STDERR: {result.stderr[-1000:]}")
        if result.stdout:
            log.info(f"STDOUT: {result.stdout[-800:]}")
    except subprocess.TimeoutExpired:
        log.error("❌ Zaman aşımı (10 dk) — Claude API geç yanıt verdi")
    except Exception as e:
        log.error(f"❌ Beklenmeyen hata: {e}")


def main():
    log.info("=" * 55)
    log.info("🚀 Günlük Bülten Scheduler v2.0 (Railway)")
    log.info(f"   Hedef: Her gün {TARGET_HOUR:02d}:{TARGET_MINUTE:02d} TR saati")
    log.info("   Kaynaklar: Claude AI + ChemOrbis + Fibre2Fashion + Sedat Sezer")
    log.info("=" * 55)

    while True:
        nxt    = next_run_time()
        now    = datetime.now(TZ_TR)
        wait_s = (nxt - now).total_seconds()

        log.info(f"⏰ Sonraki çalışma: {nxt.strftime('%Y-%m-%d %H:%M:%S')} TR")
        log.info(f"   Bekleme: {wait_s/3600:.1f} saat")

        while True:
            remaining = (next_run_time() - datetime.now(TZ_TR)).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 300))

        run_bulten()


if __name__ == "__main__":
    main()
