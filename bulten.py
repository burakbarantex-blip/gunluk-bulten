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
        "brent":    "BZ=F",
        "gold_oz":  "GC=F",
        "cotton":   "CT=F",
        "usd_try":  "USDTRY=X",
        "eur_try":  "EURTRY=X",
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
    # Genel ekonomi
    ("google_tr",       "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx1YlY4U0FuUnlHZ0pVVWlnQVAB?hl=tr&gl=TR&ceid=TR:tr"),
    ("bloomberght",     "https://www.bloomberght.com/rss"),
    # Tekstil & lif
    ("fibre2fashion",   "https://www.fibre2fashion.com/rss/news.xml"),
    ("fibre2fashion_m", "https://www.fibre2fashion.com/rss/market-report.xml"),
    # Petrokimya & hammadde
    ("chemorbis",       "https://www.chemorbis.com/en/rss/news"),
    ("chemorbis_pp",    "https://www.chemorbis.com/en/rss/polypropylene"),
    ("chemorbis_pet",   "https://www.chemorbis.com/en/rss/pet"),
    # Sedat Sezer (YouTube RSS)
    ("sedatsezer_yt",   "https://www.youtube.com/feeds/videos.xml?channel_id=UCmVBmHxFwfCnWqhBMM7gPFQ"),
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

    news_text = ""
    for n in news_items[:20]:
        news_text += f"[{n['source'].upper()}] {n['title']}\n"
        if n["summary"]:
            news_text += f"  Özet: {n['summary'][:200]}\n"

    prompt = f"""Sen Türkiye'nin önde gelen satınalma ve uluslararası ticaret uzmanısın.
Bugün {today_str} tarihli günlük bülteni hazırlıyorsun.
Şirketi: Gümuşsuyu (halı, tekstil hammaddeleri ithalatçısı — PP, PTA, MEG, viskoz, polyester)

──── FİNANSAL VERİLER (otomatik çekildi) ────
{chr(10).join(fin_summary)}

──── GÜNCEL HABERLER (Fibre2Fashion, ChemOrbis, Sedat Sezer, Bloomberg) ────
{news_text}

Aşağıdaki bölümleri Türkçe olarak hazırla. Her bölüm için JSON formatında yanıt ver.

{{
  "ozet": "3-4 cümle yönetici özeti. Bugünün en kritik 2-3 gelişmesi.",

  "dunya_gundemi": [
    {{"baslik": "haber başlığı", "kaynak": "kaynak adı", "onemi": "satınalma/ithalat açısından önemi 1 cümle"}}
  ],

  "hammadde_analiz": {{
    "pp": {{"fiyat": "...", "yon": "↑/→/↓", "yorum": "güncel yorum"}},
    "pta": {{"fiyat": "...", "yon": "↑/→/↓", "yorum": "..."}},
    "meg": {{"fiyat": "...", "yon": "↑/→/↓", "yorum": "..."}},
    "akrilonitril": {{"fiyat": "...", "yon": "↑/→/↓", "yorum": "..."}},
    "viskoz": {{"fiyat": "...", "yon": "↑/→/↓", "yorum": "..."}},
    "polyester_dty": {{"fiyat": "...", "yon": "↑/→/↓", "yorum": "..."}},
    "pvc": {{"fiyat": "...", "yon": "↑/→/↓", "yorum": "..."}}
  }},

  "lojistik": {{
    "konteyner_40hq": {{"fiyat": "...", "yon": "↑/→/↓", "yorum": "Çin→Türkiye güncel durum"}}
  }},

  "hali_sektoru": {{
    "gelismeler": ["madde 1", "madde 2", "madde 3"],
    "hammadde_etkisi": "hammadde fiyatlarının halı sektörüne etkisi"
  }},

  "sektorel_analiz": {{
    "baslik": "günün en kritik sektörel başlığı",
    "maddeler": ["madde 1", "madde 2", "madde 3"],
    "oneri": "satınalma ekibi için somut aksiyon önerisi"
  }},

  "fibre2fashion_ozet": "Fibre2Fashion'dan bugünün en önemli 1-2 gelişmesi",
  "chemorbis_ozet": "ChemOrbis'ten PP/PTA/MEG için kritik fiyat hareketi",
  "sedatsezer_ozet": "Sedat Sezer'den varsa güncel yorum/video özeti"
}}

SADECE JSON döndür. Hiçbir açıklama veya markdown ekleme."""
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
            max_tokens=4000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            system=(
                "Sen Türkiye'nin en iyi satınalma ve hammadde piyasası uzmanısın. "
                "PP, PTA, MEG, viskoz, polyester, navlun ve halı sektörü konularında "
                "derinlemesine bilgin var. Web search kullanarak güncel verileri doğrula. "
                "Fibre2Fashion, ChemOrbis ve Sedat Sezer kanallarını öncelikli kaynak olarak kullan. "
                "Yanıtını SADECE geçerli JSON formatında ver."
            ),
            messages=[{"role": "user", "content": prompt}],
        )

        # Yanıtı parse et
        full_text = ""
        for block in response.content:
            if block.type == "text":
                full_text += block.text

        # JSON temizle
        clean = full_text.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        clean = clean.strip().rstrip("```").strip()

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
        ("usd_try",     "USD/TRY",       "₺"),
        ("eur_try",     "EUR/TRY",       "₺"),
        ("gold_oz",     "Altın Ons",     " $"),
        ("gold_gram_tl","Gram Altın",    " ₺"),
        ("brent",       "Brent Petrol",  " $"),
        ("cotton",      "Pamuk ICE",     " c/lb"),
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
    loj = analysis.get("lojistik", {}).get("konteyner_40hq", {})

    # Halı sektörü
    hali = analysis.get("hali_sektoru", {})
    hali_html = "".join(f"<li>{m}</li>" for m in hali.get("gelismeler", []))

    # Sektörel analiz
    sek = analysis.get("sektorel_analiz", {})
    sek_html = "".join(f"<li style='margin-bottom:6px;'>{m}</li>" for m in sek.get("maddeler", []))

    # Kaynak özetleri
    f2f    = analysis.get("fibre2fashion_ozet", "—")
    chem   = analysis.get("chemorbis_ozet", "—")
    sedat  = analysis.get("sedatsezer_ozet", "—")

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
  <h2 style="margin:0 0 10px;color:#5c6bc0;font-size:14px;border-bottom:1px solid #e8eaf6;padding-bottom:6px;">🔍 KAYNAK ÖZETLERI</h2>
  <table width="100%" cellpadding="8" cellspacing="4">
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
  </table>
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
      <td><b>40HQ Konteyner</b></td>
      <td>{loj.get('fiyat','—')}</td>
      <td style="font-size:18px;color:{trend_color(loj.get('yon','→'))};">{loj.get('yon','→')}</td>
      <td>{loj.get('yorum','—')}</td>
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

# ─── 5. E-POSTA GÖNDER ─────────────────────────────────────────────────────

def send_email(cfg, subject, html_body):
    sender     = cfg["email"]["sender"]
    password   = cfg["email"]["app_password"]
    smtp_srv   = cfg["email"]["smtp_server"]
    smtp_port  = cfg["email"]["smtp_port"]
    recipients = cfg["recipients"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Piyasa Analiz <{sender}>"
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(smtp_srv, smtp_port, context=ctx) as srv:
            srv.login(sender, password)
            srv.sendmail(sender, recipients, msg.as_string())
        log.info(f"E-posta {len(recipients)} kişiye gönderildi.")
        return True
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

