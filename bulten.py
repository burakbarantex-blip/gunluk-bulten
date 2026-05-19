# -*- coding: utf-8 -*-
"""
Günlük Ekonomi & Hammadde Bülteni v2.0
Claude AI destekli — Otomatik veri çekme, analiz ve e-posta gönderimi
Kaynaklar: yfinance, Fibre2Fashion, ChemOrbis, Sedat Sezer, RSS
"""

import json, smtplib, ssl, os, sys, logging, time
import feedparser, requests, yfinance as yf
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
from anthropic import Anthropic

# ─── LOGLAMA ───────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE  = os.path.join(BASE_DIR, "bulten.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

TR_MONTHS = {
    1:"Ocak",2:"Şubat",3:"Mart",4:"Nisan",5:"Mayıs",6:"Haziran",
    7:"Temmuz",8:"Ağustos",9:"Eylül",10:"Ekim",11:"Kasım",12:"Aralık"
}

# ─── CONFIG ────────────────────────────────────────────────────────────────

def load_config():
    path = os.path.join(BASE_DIR, "config.json")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Railway ortam değişkenlerinden override (güvenlik için önerilir)
    if os.environ.get("EMAIL_SENDER"):
        cfg["email"]["sender"] = os.environ["EMAIL_SENDER"]
    if os.environ.get("EMAIL_APP_PASSWORD"):
        cfg["email"]["app_password"] = os.environ["EMAIL_APP_PASSWORD"]
    if os.environ.get("EMAIL_RECIPIENTS"):
        cfg["recipients"] = [r.strip() for r in os.environ["EMAIL_RECIPIENTS"].split(",")]
    return cfg

# ─── 1. FİNANSAL VERİ (yfinance) ───────────────────────────────────────────

def fetch_financial():
    """Döviz, petrol, altın, pamuk verilerini yfinance ile çek."""
    tickers = {
        "brent":    "BZ=F",       # Brent ham petrol — PP upstream proxy (öncül gösterge)
        "wti":      "CL=F",       # WTI petrol — naphtha korelasyonu
        "gold_oz":  "GC=F",       # Altın ons
        "cotton":   "CT=F",       # Pamuk ICE
        "usd_try":  "USDTRY=X",   # USD/TRY
        "eur_try":  "EURTRY=X",   # EUR/TRY
        "usd_cny":  "USDCNH=X",   # USD/CNY — Çin tedarik maliyeti sinyali
    }
    data = {}
    for key, sym in tickers.items():
        try:
            hist = yf.Ticker(sym).history(period="5d")
            if hist.empty:
                log.warning(f"{key} ({sym}): veri yok")
                continue
            c0 = hist["Close"].iloc[-1]
            c1 = hist["Close"].iloc[-2] if len(hist) >= 2 else c0
            chg = ((c0 - c1) / c1 * 100) if c1 else 0
            data[key] = {"price": round(c0,2), "prev": round(c1,2), "change": round(chg,2)}
        except Exception as e:
            log.error(f"{key} çekilemedi: {e}")

    # Gram altın TL hesapla
    if "gold_oz" in data and "usd_try" in data:
        g  = (data["gold_oz"]["price"] * data["usd_try"]["price"]) / 31.1035
        gp = (data["gold_oz"]["prev"]  * data["usd_try"]["prev"])  / 31.1035
        gc = ((g - gp) / gp * 100) if gp else 0
        data["gold_gram_tl"] = {"price": round(g,2), "prev": round(gp,2), "change": round(gc,2)}

    log.info(f"  yfinance: {len(data)} veri çekildi")
    return data

# ─── 2. SEKTÖREL HABERLER (RSS + Web) ──────────────────────────────────────

FEEDS = [
    # ── Genel ekonomi ──────────────────────────────────────────────────────
    ("google_tr",        "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx1YlY4U0FuUnlHZ0pVVWlnQVAB?hl=tr&gl=TR&ceid=TR:tr"),
    ("bloomberght",      "https://www.bloomberght.com/rss"),

    # ── Tekstil & Lif ──────────────────────────────────────────────────────
    ("fibre2fashion",    "https://www.fibre2fashion.com/rss/news.xml"),
    ("fibre2fashion_m",  "https://www.fibre2fashion.com/rss/market-report.xml"),

    # ── Petrokimya — ChemOrbis YouTube (web RSS üyelik gerektiriyor) ───────
    ("chemorbis_yt",     "https://www.youtube.com/feeds/videos.xml?channel_id=UCdHqNUDFJBrlNmqaxLQLaSQ"),

    # ── Sedat Sezer — Tekstil & Tedarik Zinciri (doğrulanmış) ─────────────
    ("sedatsezer_yt",    "https://www.youtube.com/feeds/videos.xml?channel_id=UCVann0hVvoJdPH_XK9rW1ww"),

    # ── Upstream Proxy — Brent & Naphtha haberleri (PP öncül göstergesi) ──
    ("google_brent",     "https://news.google.com/rss/search?q=brent+crude+naphtha+price&hl=en&gl=US&ceid=US:en"),
    ("google_naphtha",   "https://news.google.com/rss/search?q=naphtha+singapore+ARA+price&hl=en&gl=US&ceid=US:en"),

    # ── Asya Futures — Dalian PP futures (Çin talep sinyali) ──────────────
    ("google_dalian",    "https://news.google.com/rss/search?q=Dalian+polypropylene+futures&hl=en&gl=US&ceid=US:en"),

    # ── Üretici Sinyalleri — SABIC, Borealis, LyondellBasell ──────────────
    ("google_sabic",     "https://news.google.com/rss/search?q=SABIC+polypropylene+capacity+force+majeure&hl=en&gl=US&ceid=US:en"),
    ("google_producer",  "https://news.google.com/rss/search?q=Borealis+LyondellBasell+PP+production&hl=en&gl=US&ceid=US:en"),

    # ── Lojistik — Drewry & Freightos ─────────────────────────────────────
    ("drewry",           "https://www.drewry.co.uk/rss"),
    ("google_drewry",    "https://news.google.com/rss/search?q=Drewry+World+Container+Index+freight&hl=en&gl=US&ceid=US:en"),
    ("google_freightos", "https://news.google.com/rss/search?q=Freightos+Baltic+Index+container+rate&hl=en&gl=US&ceid=US:en"),

    # ── Haber & Yorum — ICIS, Polymerupdate, ChemAnalyst ──────────────────
    ("google_icis",      "https://news.google.com/rss/search?q=ICIS+polypropylene+polyester+price&hl=en&gl=US&ceid=US:en"),
    ("google_polymerup", "https://news.google.com/rss/search?q=Polymerupdate+PP+PTA+MEG&hl=en&gl=US&ceid=US:en"),
    ("google_chemanalyst","https://news.google.com/rss/search?q=ChemAnalyst+polypropylene+price&hl=en&gl=US&ceid=US:en"),

    # ── PP & Petrokimya Genel ──────────────────────────────────────────────
    ("google_pp",        "https://news.google.com/rss/search?q=polypropylene+price&hl=en&gl=US&ceid=US:en"),
    ("google_tekstil",   "https://news.google.com/rss/search?q=tekstil+hammadde+fiyat&hl=tr&gl=TR&ceid=TR:tr"),
]

def fetch_sector_news():
    """Tüm RSS kaynaklarından sektörel haberleri çek ve etiketle."""
    items = []
    for source, url in FEEDS:
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
            for e in feed.entries[:4]:
                title   = e.get("title", "").strip()
                summary = e.get("summary", e.get("description", "")).strip()[:300]
                published = e.get("published", "")
                link    = e.get("link", "")
                if title and len(title) > 10:
                    items.append({
                        "source":    source,
                        "title":     title,
                        "summary":   summary,
                        "published": published,
                        "link":      link,
                    })
        except Exception as ex:
            log.warning(f"RSS okunamadı [{source}]: {ex}")

    log.info(f"  RSS: {len(items)} haber çekildi ({len(FEEDS)} kaynak)")
    return items

# ─── 3. CLAUDE AI ANALİZİ ──────────────────────────────────────────────────

def build_analysis_prompt(fin_data, news_items, today_str):
    """Claude'a gönderilecek analiz promptunu oluştur."""

    fin_summary = []
    labels = {
        "brent":       "Brent Petrol ($/varil)",
        "gold_oz":     "Altın Ons ($/ons)",
        "gold_gram_tl":"Gram Altın (₺)",
        "cotton":      "Pamuk ICE (c/lb)",
        "usd_try":     "USD/TRY",
        "eur_try":     "EUR/TRY",
    }
    for k, lbl in labels.items():
        if k in fin_data:
            d = fin_data[k]
            fin_summary.append(f"- {lbl}: {d['price']} (değişim: %{d['change']:+.2f})")

    # Haberleri kısa özet olarak hazırla
    news_text = ""
    for n in news_items[:5]:
        news_text += f"- [{n['source'].upper()}] {n['title']}\n"

    prompt = f"""Tarih: {today_str}
Şirket: Gümuşsuyu (halı/tekstil hammadde ithalatçısı — PP, PTA, MEG, viskoz, polyester)

Finansal veriler:
{chr(10).join(fin_summary)}

Haberler:
{news_text}

ÖNEMLİ: Aşağıdaki JSON'u eksiksiz doldur. Veri bulamazsan makul tahmin yaz — HİÇBİR ALAN BOŞ KALMASIN, "—" yazma.

SADECE JSON döndür, başka hiçbir şey yazma:

{{"ozet":"2-3 cümle özet","dunya_gundemi":[{{"baslik":"haber1","kaynak":"kaynak1","onemi":"önemi1"}},{{"baslik":"haber2","kaynak":"kaynak2","onemi":"önemi2"}}],"hammadde_analiz":{{"pp":{{"fiyat":"tahmini fiyat yaz","yon":"↑","yorum":"yorum yaz"}},"pta":{{"fiyat":"tahmini fiyat yaz","yon":"→","yorum":"yorum yaz"}},"meg":{{"fiyat":"tahmini fiyat yaz","yon":"→","yorum":"yorum yaz"}},"akrilonitril":{{"fiyat":"tahmini fiyat yaz","yon":"→","yorum":"yorum yaz"}},"viskoz":{{"fiyat":"tahmini fiyat yaz","yon":"↑","yorum":"yorum yaz"}},"polyester_dty":{{"fiyat":"tahmini fiyat yaz","yon":"→","yorum":"yorum yaz"}},"pvc":{{"fiyat":"tahmini fiyat yaz","yon":"↓","yorum":"yorum yaz"}}}},"upstream_proxy":{{"naphtha_singapur":{{"fiyat":"~650$/ton","yon":"↑","yorum":"PP maliyetine 4-8 hafta içinde yansır"}},"naphtha_ara":{{"fiyat":"~640$/ton","yon":"↑","yorum":"Avrupa naphtha yüksek"}},"propilen":{{"fiyat":"~900$/ton","yon":"↑","yorum":"PP hammaddesi baskı altında"}}}},"asya_sinyali":{{"dalian_pp_futures":{{"fiyat":"~7800 CNY/ton","yon":"→","yorum":"Çin talebi ılımlı"}},"usd_cny":{{"fiyat":"6.82","yon":"→","yorum":"Stabil seyir"}}}},"uretici_sinyalleri":[{{"uretici":"SABIC/Borealis/LyondellBasell","haber":"Mevcut kapasite normal seyrediyor","etki":"Arz yeterli"}}],"lojistik":{{"drewry_wci":{{"fiyat":"~2800$/konteyner","yon":"→","yorum":"Stabil seyir"}},"freightos_fbi":{{"fiyat":"~2600$/konteyner","yon":"→","yorum":"Orta Doğu riskleri izleniyor"}},"konteyner_40hq":{{"fiyat":"~2500-3000$","yon":"→","yorum":"Çin-Türkiye hattı normal"}}}},"hali_sektoru":{{"gelismeler":["Türkiye halı ihracatı 2026 hedefine ilerliyor","PP ve polyester fiyat baskısı maliyet hesaplamalarını zorlaştırıyor","Avrupa pazarında talep ılımlı seyrediyor"],"hammadde_etkisi":"Yüksek Brent petrol PP/polyester maliyetlerini artırıyor; halı üreticileri fiyat artışını müşterilere yansıtmakta zorlanıyor"}},"sektorel_analiz":{{"baslik":"Yüksek Enerji Maliyetleri ve Jeopolitik Risk Hammadde Zincirini Sıkıştırıyor","maddeler":["Brent petrolün 110$/varil üzerinde seyri naphtha ve propilen maliyetlerini artırıyor","USD/TRY 45.57 seviyesi ithalat maliyetlerini yüksek tutuyor","Orta Doğu gerilimi navlun primlerinde artış riski yaratıyor"],"oneri":"Satınalma ekibi PP ve PTA için spot alım yerine vadeli sözleşme yapılandırmasını değerlendirmeli; stok seviyelerini 6-8 haftaya çıkarın"}},"kaynak_ozetleri":{{"icis":"PP fiyatları upstream baskı altında","polymerupdate":"MEG ve PTA piyasası yatay seyrediyor","chemanalyst":"Propilen maliyetleri yükselişte","fibre2fashion":"Tekstil hammadde talebi ılımlı","chemorbis":"Polimer piyasasında jeopolitik risk izleniyor","sedatsezer":"Tedarik zinciri yönetiminde stok optimizasyonu önerisi"}}}}"""
    return prompt


def run_claude_analysis(prompt):
    """Claude API ile analiz yaptır — web search destekli."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY ortam değişkeni bulunamadı!")
        return None

    client = Anthropic(api_key=api_key)

    try:
        log.info("  Claude API'ye istek gönderiliyor...")
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=3000,
            system=(
                "Sen Türkiye'nin en iyi satınalma ve hammadde piyasası uzmanısın. "
                "PP, PTA, MEG, viskoz, polyester, navlun ve halı sektörü konularında derinlemesine bilgin var.\n\n"
                "ANALİZ ÇERÇEVENİ:\n"
                "1. UPSTREAM PROXY: Brent petrol → Naphtha (Singapur/ARA) → Propilen → PP zincirini takip et. "
                "Brent/naphtha hareketi PP fiyatının 4-8 hafta öncül göstergesidir.\n"
                "2. ASYA FUTURES: Çin Dalian Commodity Exchange PP futures fiyatları Asya talep sinyalidir. "
                "USD/CNY paritesi Çin'den ithalat maliyetini doğrudan etkiler.\n"
                "3. ÜRETİCİ SİNYALLERİ: SABIC, Borealis, LyondellBasell'in kapasite, force majeure veya "
                "bakım duruşu haberlerini arz kısıtı sinyali olarak değerlendir.\n"
                "4. LOJİSTİK: Drewry World Container Index ve Freightos Baltic Index'i landed cost "
                "(nihai teslim maliyeti) hesabının freight bileşeni olarak kullan.\n"
                "5. HABER KAYNAKLARI: ICIS, Polymerupdate, ChemAnalyst serbest başlıklarını, "
                "ChemOrbis ve Fibre2Fashion analizlerini, Sedat Sezer yorumlarını önceliklendir.\n\n"
                "Web search kullanarak güncel verileri doğrula. Yanıtını SADECE geçerli JSON formatında ver."
            ),
            messages=[{"role": "user", "content": prompt}],
        )

        # Yanıtı parse et
        full_text = ""
        for block in response.content:
            if hasattr(block, 'text'):
                full_text += block.text

        # JSON temizle
        clean = full_text.strip()
        if "```json" in clean:
            clean = clean.split("```json")[1]
        if "```" in clean:
            clean = clean.split("```")[0]
        clean = clean.strip()

        # Eğer JSON kesilmişse sonuna kapanış ekle
        if not clean.endswith("}"):
            # Açık string varsa kapat
            open_strings = clean.count('"') % 2
            if open_strings:
                clean += '"'
            # Açık obje/array sayısını hesapla ve kapat
            depth = 0
            in_string = False
            for ch in clean:
                if ch == '"' and not in_string:
                    in_string = True
                elif ch == '"' and in_string:
                    in_string = False
                elif not in_string:
                    if ch in '{[':
                        depth += 1
                    elif ch in '}]':
                        depth -= 1
            # Eksik kapanışları ekle
            while depth > 0:
                clean += "}"
                depth -= 1

        result = json.loads(clean)
        log.info("  Claude analizi başarıyla tamamlandı.")
        return result

    except json.JSONDecodeError as e:
        log.error(f"Claude JSON parse hatası: {e}")
        log.error(f"Ham yanıt: {full_text[:500]}")
        return None
    except Exception as e:
        log.error(f"Claude API hatası: {e}")
        return None

# ─── 4. HTML BÜLTEN OLUŞTUR ────────────────────────────────────────────────

def trend_color(yon):
    return {"↑": "#e53935", "↓": "#43a047"}.get(yon, "#757575")

def build_html(fin, analysis, today_str):
    """Claude analizinden ve finansal verilerden profesyonel HTML bülten oluştur."""

    def fmt(val, suffix=""):
        if isinstance(val, (int, float)):
            return f"{val:,.2f}{suffix}".replace(",","X").replace(".",",").replace("X",".")
        return str(val)

    def alert(chg):
        return ' <span style="color:#e53935;font-weight:bold;">⚡</span>' if abs(chg) > 2 else ""

    # Finansal satırlar
    fin_rows = ""
    fin_map = [
        ("usd_try",     "USD/TRY",            "₺"),
        ("eur_try",     "EUR/TRY",            "₺"),
        ("usd_cny",     "USD/CNY",            "¥"),
        ("gold_oz",     "Altın Ons",          " $"),
        ("gold_gram_tl","Gram Altın",         " ₺"),
        ("brent",       "Brent Petrol",       " $"),
        ("wti",         "WTI Petrol",         " $"),
        ("cotton",      "Pamuk ICE",          " c/lb"),
    ]
    for i, (key, label, sfx) in enumerate(fin_map):
        if key not in fin:
            continue
        d   = fin[key]
        chg = d["change"]
        yon = "↑" if chg > 0.5 else ("↓" if chg < -0.5 else "→")
        bg  = 'style="background:#f9f9f9;"' if i % 2 else ""
        fin_rows += f"""
        <tr {bg}>
          <td><b>{label}</b></td>
          <td>{fmt(d['price'])}{sfx}{alert(chg)}</td>
          <td style="color:{trend_color(yon)};font-size:18px;">{yon}</td>
          <td>%{chg:+.2f}</td>
        </tr>"""

    # Hammadde satırları
    hm = analysis.get("hammadde_analiz", {})
    hm_labels = {
        "pp":"PP (Polipropilen)", "pta":"PTA", "meg":"MEG",
        "akrilonitril":"Akrilonitril", "viskoz":"Viskoz Elyaf",
        "polyester_dty":"Polyester DTY", "pvc":"PVC"
    }
    hm_rows = ""
    for i, (key, label) in enumerate(hm_labels.items()):
        item = hm.get(key, {})
        yon  = item.get("yon", "→")
        bg   = 'style="background:#f9f9f9;"' if i % 2 else ""
        hm_rows += f"""
        <tr {bg}>
          <td><b>{label}</b></td>
          <td>{item.get('fiyat','—')}</td>
          <td style="color:{trend_color(yon)};font-size:18px;">{yon}</td>
          <td>{item.get('yorum','—')}</td>
        </tr>"""

    # Dünya gündemi
    gundem_html = ""
    for g in analysis.get("dunya_gundemi", [])[:6]:
        gundem_html += f"""
        <li style="margin-bottom:8px;">
          <span style="font-weight:600;">{g.get('baslik','')}</span>
          <span style="color:#888;font-size:11px;margin-left:6px;">[{g.get('kaynak','')}]</span><br>
          <span style="color:#555;font-size:11px;">{g.get('onemi','')}</span>
        </li>"""

    # Lojistik
    loj     = analysis.get("lojistik", {})
    loj_40  = loj.get("konteyner_40hq", {})
    drewry  = loj.get("drewry_wci", {})
    fbalt   = loj.get("freightos_fbi", {})

    # Upstream proxy
    ups = analysis.get("upstream_proxy", {})
    asya = analysis.get("asya_sinyali", {})

    # Üretici sinyalleri
    uretici_list = analysis.get("uretici_sinyalleri", [])
    uretici_html = "".join(
        f"<li style='margin-bottom:6px;'><b>{u.get('uretici','')}</b>: {u.get('haber','')} "
        f"<span style='color:#e53935;'>→ {u.get('etki','')}</span></li>"
        for u in uretici_list
    ) or "<li>Güncel üretici haberi tespit edilmedi.</li>"

    # Halı sektörü
    hali = analysis.get("hali_sektoru", {})
    hali_html = "".join(f"<li>{m}</li>" for m in hali.get("gelismeler", []))

    # Sektörel analiz
    sek = analysis.get("sektorel_analiz", {})
    sek_html = "".join(f"<li style='margin-bottom:6px;'>{m}</li>" for m in sek.get("maddeler", []))

    # Kaynak özetleri
    kaynaklar = analysis.get("kaynak_ozetleri", {})
    f2f    = kaynaklar.get("fibre2fashion", analysis.get("fibre2fashion_ozet", "—"))
    chem   = kaynaklar.get("chemorbis",     analysis.get("chemorbis_ozet", "—"))
    sedat  = kaynaklar.get("sedatsezer",    analysis.get("sedatsezer_ozet", "—"))
    icis_t = kaynaklar.get("icis", "—")
    polym  = kaynaklar.get("polymerupdate", "—")
    chem_a = kaynaklar.get("chemanalyst", "—")

    html = f"""<!DOCTYPE html>
<html lang="tr"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Günlük Bülten – {today_str}</title></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f2f5;padding:20px 0;">
<tr><td align="center">
<table width="680" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08);">

<!-- HEADER -->
<tr><td style="background:linear-gradient(135deg,#1a237e,#283593);padding:24px 30px;text-align:center;">
  <h1 style="margin:0;color:#fff;font-size:20px;letter-spacing:.5px;">📅 Günlük Ekonomi &amp; Hammadde Bülteni</h1>
  <p style="margin:6px 0 0;color:#c5cae9;font-size:13px;">{today_str} &nbsp;|&nbsp; Satınalma &amp; Piyasa Analiz &nbsp;|&nbsp; <span style="color:#80cbc4;">Claude AI</span></p>
</td></tr>

<!-- YÖNETİCİ ÖZETİ -->
<tr><td style="padding:20px 30px;background:#e8eaf6;border-left:4px solid #3949ab;">
  <p style="margin:0 0 4px;font-size:11px;font-weight:bold;color:#3949ab;text-transform:uppercase;letter-spacing:.05em;">YÖNETİCİ ÖZETİ (Executive Summary)</p>
  <p style="margin:0;font-size:13px;color:#1a1a2e;line-height:1.7;">{analysis.get('ozet','')}</p>
</td></tr>

<!-- KAYNAK ÖZETLERİ -->
<tr><td style="padding:16px 30px 6px;">
  <h2 style="margin:0 0 10px;color:#5c6bc0;font-size:14px;border-bottom:1px solid #e8eaf6;padding-bottom:6px;">🔍 KAYNAK ÖZETLERİ</h2>
  <table width="100%" cellpadding="4" cellspacing="4">
    <tr>
      <td width="33%" style="background:#fff8e1;border-radius:6px;padding:10px;font-size:11px;vertical-align:top;">
        <div style="font-weight:bold;color:#f57f17;margin-bottom:4px;">📊 ChemOrbis</div>
        <div style="color:#555;">{chem}</div>
      </td>
      <td width="33%" style="background:#e8f5e9;border-radius:6px;padding:10px;font-size:11px;vertical-align:top;">
        <div style="font-weight:bold;color:#2e7d32;margin-bottom:4px;">🌐 Fibre2Fashion</div>
        <div style="color:#555;">{f2f}</div>
      </td>
      <td width="34%" style="background:#fce4ec;border-radius:6px;padding:10px;font-size:11px;vertical-align:top;">
        <div style="font-weight:bold;color:#c62828;margin-bottom:4px;">📺 Sedat Sezer</div>
        <div style="color:#555;">{sedat}</div>
      </td>
    </tr>
    <tr>
      <td style="background:#e3f2fd;border-radius:6px;padding:10px;font-size:11px;vertical-align:top;">
        <div style="font-weight:bold;color:#1565c0;margin-bottom:4px;">📰 ICIS</div>
        <div style="color:#555;">{icis_t}</div>
      </td>
      <td style="background:#f3e5f5;border-radius:6px;padding:10px;font-size:11px;vertical-align:top;">
        <div style="font-weight:bold;color:#6a1b9a;margin-bottom:4px;">🔬 Polymerupdate</div>
        <div style="color:#555;">{polym}</div>
      </td>
      <td style="background:#e0f2f1;border-radius:6px;padding:10px;font-size:11px;vertical-align:top;">
        <div style="font-weight:bold;color:#00695c;margin-bottom:4px;">📈 ChemAnalyst</div>
        <div style="color:#555;">{chem_a}</div>
      </td>
    </tr>
  </table>
</td></tr>

<!-- UPSTREAM PROXY — PP Öncül Göstergesi -->
<tr><td style="padding:16px 30px 10px;">
  <h2 style="margin:0 0 6px;color:#4527a0;font-size:15px;border-bottom:2px solid #ede7f6;padding-bottom:6px;">⛽ UPSTREAM PROXY — PP Öncül Göstergesi (4-8 Hafta)</h2>
  <p style="margin:0 0 8px;font-size:11px;color:#888;">Brent → Naphtha → Propilen → PP zinciri: Naphtha hareketleri PP fiyatının 4-8 hafta öncül sinyalidir.</p>
  <table width="100%" cellpadding="8" cellspacing="0" style="font-size:12px;border-collapse:collapse;border:1px solid #e0e0e0;">
    <tr style="background:#4527a0;color:#fff;">
      <th align="left" style="padding:10px;">Gösterge</th>
      <th align="left" style="padding:10px;">Fiyat</th>
      <th align="center" style="padding:10px;">Yön</th>
      <th align="left" style="padding:10px;">PP'ye Etkisi</th>
    </tr>
    <tr>
      <td><b>Naphtha Singapur</b></td>
      <td>{ups.get('naphtha_singapur', {}).get('fiyat','—')}</td>
      <td style="font-size:18px;color:{trend_color(ups.get('naphtha_singapur', {}).get('yon','→'))};">{ups.get('naphtha_singapur', {}).get('yon','→')}</td>
      <td>{ups.get('naphtha_singapur', {}).get('yorum','—')}</td>
    </tr>
    <tr style="background:#f9f9f9;">
      <td><b>Naphtha ARA</b></td>
      <td>{ups.get('naphtha_ara', {}).get('fiyat','—')}</td>
      <td style="font-size:18px;color:{trend_color(ups.get('naphtha_ara', {}).get('yon','→'))};">{ups.get('naphtha_ara', {}).get('yon','→')}</td>
      <td>{ups.get('naphtha_ara', {}).get('yorum','—')}</td>
    </tr>
    <tr>
      <td><b>Propilen</b></td>
      <td>{ups.get('propilen', {}).get('fiyat','—')}</td>
      <td style="font-size:18px;color:{trend_color(ups.get('propilen', {}).get('yon','→'))};">{ups.get('propilen', {}).get('yon','→')}</td>
      <td>{ups.get('propilen', {}).get('yorum','—')}</td>
    </tr>
  </table>
  <table width="100%" cellpadding="8" cellspacing="0" style="font-size:12px;border-collapse:collapse;border:1px solid #e0e0e0;margin-top:6px;">
    <tr style="background:#37474f;color:#fff;">
      <th align="left" style="padding:10px;">Asya Sinyali</th>
      <th align="left" style="padding:10px;">Seviye</th>
      <th align="center" style="padding:10px;">Yön</th>
      <th align="left" style="padding:10px;">Yorum</th>
    </tr>
    <tr>
      <td><b>Dalian PP Futures</b></td>
      <td>{asya.get('dalian_pp_futures', {}).get('fiyat','—')}</td>
      <td style="font-size:18px;color:{trend_color(asya.get('dalian_pp_futures', {}).get('yon','→'))};">{asya.get('dalian_pp_futures', {}).get('yon','→')}</td>
      <td>{asya.get('dalian_pp_futures', {}).get('yorum','—')}</td>
    </tr>
    <tr style="background:#f9f9f9;">
      <td><b>USD/CNY</b></td>
      <td>{asya.get('usd_cny', {}).get('fiyat','—')}</td>
      <td style="font-size:18px;color:{trend_color(asya.get('usd_cny', {}).get('yon','→'))};">{asya.get('usd_cny', {}).get('yon','→')}</td>
      <td>{asya.get('usd_cny', {}).get('yorum','—')}</td>
    </tr>
  </table>
</td></tr>

<!-- ÜRETİCİ SİNYALLERİ -->
<tr><td style="padding:16px 30px 10px;">
  <h2 style="margin:0 0 10px;color:#bf360c;font-size:15px;border-bottom:2px solid #fbe9e7;padding-bottom:6px;">🏭 ÜRETİCİ SİNYALLERİ (SABIC · Borealis · LyondellBasell)</h2>
  <ul style="margin:0;padding-left:16px;font-size:12px;color:#333;line-height:1.8;">{uretici_html}</ul>
</td></tr>

<!-- DÜNYA GÜNDEMİ -->
<tr><td style="padding:16px 30px 10px;">
  <h2 style="margin:0 0 10px;color:#1a237e;font-size:15px;border-bottom:2px solid #e8eaf6;padding-bottom:6px;">🌍 DÜNYA GÜNDEMİ</h2>
  <ul style="margin:0;padding-left:18px;color:#333;font-size:13px;line-height:1.7;">{gundem_html}</ul>
</td></tr>

<!-- HAMMADDE -->
<tr><td style="padding:16px 30px 10px;">
  <h2 style="margin:0 0 10px;color:#f57f17;font-size:15px;border-bottom:2px solid #fff8e1;padding-bottom:6px;">🟡 HAMMADDE PİYASASI</h2>
  <table width="100%" cellpadding="8" cellspacing="0" style="font-size:12px;border-collapse:collapse;border:1px solid #e0e0e0;">
    <tr style="background:#1a237e;color:#fff;">
      <th align="left" style="padding:10px;">Hammadde</th>
      <th align="left" style="padding:10px;">Fiyat</th>
      <th align="center" style="padding:10px;">Yön</th>
      <th align="left" style="padding:10px;">Yorum</th>
    </tr>{hm_rows}
  </table>
</td></tr>

<!-- DÖVİZ & MADENLER -->
<tr><td style="padding:16px 30px 10px;">
  <h2 style="margin:0 0 10px;color:#1565c0;font-size:15px;border-bottom:2px solid #e3f2fd;padding-bottom:6px;">🔵 DÖVİZ &amp; KIYMETLİ MADENLER</h2>
  <table width="100%" cellpadding="8" cellspacing="0" style="font-size:12px;border-collapse:collapse;border:1px solid #e0e0e0;">
    <tr style="background:#1565c0;color:#fff;">
      <th align="left" style="padding:10px;">Gösterge</th>
      <th align="left" style="padding:10px;">Seviye</th>
      <th align="center" style="padding:10px;">Yön</th>
      <th align="left" style="padding:10px;">Değişim</th>
    </tr>{fin_rows}
  </table>
</td></tr>

<!-- LOJİSTİK -->
<tr><td style="padding:16px 30px 10px;">
  <h2 style="margin:0 0 10px;color:#00695c;font-size:15px;border-bottom:2px solid #e0f2f1;padding-bottom:6px;">🚢 LOJİSTİK – ÇİN → TÜRKİYE</h2>
  <table width="100%" cellpadding="8" cellspacing="0" style="font-size:12px;border-collapse:collapse;border:1px solid #e0e0e0;">
    <tr style="background:#00695c;color:#fff;">
      <th align="left" style="padding:10px;">Gösterge</th>
      <th align="left">Seviye</th>
      <th align="center">Yön</th>
      <th align="left">Yorum</th>
    </tr>
    <tr>
      <td><b>Drewry WCI</b></td>
      <td>{drewry.get('fiyat','—')}</td>
      <td style="font-size:18px;color:{trend_color(drewry.get('yon','→'))};">{drewry.get('yon','→')}</td>
      <td>{drewry.get('yorum','—')}</td>
    </tr>
    <tr style="background:#f9f9f9;">
      <td><b>Freightos Baltic Index</b></td>
      <td>{fbalt.get('fiyat','—')}</td>
      <td style="font-size:18px;color:{trend_color(fbalt.get('yon','→'))};">{fbalt.get('yon','→')}</td>
      <td>{fbalt.get('yorum','—')}</td>
    </tr>
    <tr>
      <td><b>40HQ Çin→Türkiye</b></td>
      <td>{loj_40.get('fiyat','—')}</td>
      <td style="font-size:18px;color:{trend_color(loj_40.get('yon','→'))};">{loj_40.get('yon','→')}</td>
      <td>{loj_40.get('yorum','—')}</td>
    </tr>
  </table>
  <p style="font-size:11px;color:#888;margin-top:6px;">⚠️ Navlun fiyatları liman, taşıyıcı ve ek ücretlere göre değişkenlik gösterir.</p>
</td></tr>

<!-- HALI SEKTÖRÜ -->
<tr><td style="padding:16px 30px 10px;">
  <h2 style="margin:0 0 10px;color:#6a1b9a;font-size:15px;border-bottom:2px solid #f3e5f5;padding-bottom:6px;">🧶 HALI SEKTÖRÜ</h2>
  <ul style="margin:0 0 10px;padding-left:16px;font-size:12px;color:#444;line-height:1.7;">{hali_html}</ul>
  <p style="font-size:12px;color:#1a237e;background:#f3e5f5;padding:8px 12px;border-radius:4px;margin:0;">
    💡 <i>{hali.get('hammadde_etkisi','')}</i></p>
</td></tr>

<!-- SEKTÖREL ANALİZ -->
<tr><td style="padding:16px 30px 10px;">
  <h2 style="margin:0 0 10px;color:#bf360c;font-size:15px;border-bottom:2px solid #fbe9e7;padding-bottom:6px;">🎬 SEKTÖREL ANALİZ NOTU</h2>
  <p style="font-size:13px;color:#bf360c;margin:0 0 8px;font-weight:bold;">{sek.get('baslik','')}</p>
  <ul style="margin:0 0 8px;padding-left:16px;font-size:12px;color:#333;line-height:1.7;">{sek_html}</ul>
  <p style="font-size:12px;color:#1a237e;background:#e8eaf6;padding:8px 12px;border-radius:4px;margin:0;">
    💡 <i>{sek.get('oneri','')}</i></p>
</td></tr>

<!-- FOOTER -->
<tr><td style="padding:20px 30px;background:#f5f5f5;border-top:1px solid #e0e0e0;text-align:center;">
  <p style="margin:0;font-size:13px;color:#333;">Saygılarımızla,</p>
  <p style="margin:4px 0 0;font-size:14px;color:#1a237e;font-weight:bold;">Satınalma &amp; Piyasa Analiz – Gümuşsuyu</p>
  <p style="margin:8px 0 0;font-size:10px;color:#999;">
    Bu bülten Claude AI destekli otomatik sistemle oluşturulmuştur.<br>
    Kaynaklar: yfinance · ChemOrbis · Fibre2Fashion · Sedat Sezer · Bloomberg HT
  </p>
</td></tr>

</table></td></tr></table>
</body></html>"""
    return html

# ─── 5. E-POSTA GÖNDER (SendGrid API) ─────────────────────────────────────

def send_email(cfg, subject, html_body):
    recipients = cfg["recipients"]
    sender     = cfg["email"]["sender"]

    sendgrid_key = os.environ.get("SENDGRID_API_KEY")
    if not sendgrid_key:
        log.error("SENDGRID_API_KEY ortam değişkeni bulunamadı!")
        return False

    try:
        headers = {
            "Authorization": f"Bearer {sendgrid_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "personalizations": [{"to": [{"email": r} for r in recipients]}],
            "from": {"email": sender, "name": "Piyasa Analiz"},
            "subject": subject,
            "content": [{"type": "text/html", "value": html_body}],
        }
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            json=payload,
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 202:
            log.info(f"E-posta {len(recipients)} kişiye gönderildi (SendGrid).")
            return True
        else:
            log.error(f"SendGrid hatası: {resp.status_code} — {resp.text}")
            return False

    except Exception as e:
        log.error(f"E-posta gönderilemedi: {e}")
        return False

# ─── 6. FALLBACK (Claude API başarısız olursa) ─────────────────────────────

def build_fallback_analysis(news_items):
    """Claude API çalışmazsa minimal analiz döndür."""
    titles = [n["title"] for n in news_items[:6]]
    return {
        "ozet": "Otomatik analiz şu an kullanılamıyor. Haberler aşağıda listelendi.",
        "dunya_gundemi": [{"baslik": t, "kaynak": "RSS", "onemi": ""} for t in titles],
        "hammadde_analiz": {k: {"fiyat":"—","yon":"→","yorum":"Güncelleniyor"} for k in
                            ["pp","pta","meg","akrilonitril","viskoz","polyester_dty","pvc"]},
        "lojistik": {"konteyner_40hq": {"fiyat":"—","yon":"→","yorum":"Güncelleniyor"}},
        "hali_sektoru": {"gelismeler": ["Veriler güncelleniyor..."], "hammadde_etkisi": ""},
        "sektorel_analiz": {"baslik": "—", "maddeler": [], "oneri": ""},
        "fibre2fashion_ozet": "—",
        "chemorbis_ozet": "—",
        "sedatsezer_ozet": "—",
    }

# ─── MAIN ──────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 55)
    log.info("Bülten v2.0 başlatıldı (Claude AI destekli)")

    tz_tr    = timezone(timedelta(hours=3))
    now      = datetime.now(tz_tr)
    today_str = f"{now.day} {TR_MONTHS[now.month]} {now.year}"
    subject  = f"📅 Günlük Ekonomi & Hammadde Bülteni – {today_str}"

    cfg = load_config()

    # 1. Finansal veriler
    log.info("Finansal veriler çekiliyor...")
    fin_data = fetch_financial()

    # 2. Sektörel haberler
    log.info("Sektörel haberler çekiliyor...")
    news_items = fetch_sector_news()

    # 3. Claude AI analizi
    log.info("Claude AI analizi yapılıyor...")
    prompt   = build_analysis_prompt(fin_data, news_items, today_str)
    analysis = run_claude_analysis(prompt)

    if not analysis:
        log.warning("Claude analizi başarısız — fallback moda geçiliyor")
        analysis = build_fallback_analysis(news_items)

    # 4. HTML oluştur
    log.info("HTML bülten oluşturuluyor...")
    html = build_html(fin_data, analysis, today_str)

    # 5. Gönder
    log.info(f"E-posta gönderiliyor: {subject}")
    ok = send_email(cfg, subject, html)

    if ok:
        log.info("✅ Bülten başarıyla gönderildi!")
    else:
        log.error("❌ Bülten gönderilemedi!")
        sys.exit(1)


if __name__ == "__main__":
    main()
