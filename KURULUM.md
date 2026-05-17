# Günlük Bülten v2.0 — Kurulum Rehberi

## Değişen Neler?

| Özellik | v1 (Antigravity) | v2 (Claude AI) |
|---|---|---|
| Hammadde yorumu | manuel_data.json (elle güncellenir) | Claude AI (otomatik + güncel) |
| Veri kaynakları | yfinance + 2 RSS | yfinance + ChemOrbis + Fibre2Fashion + Sedat Sezer |
| Analiz kalitesi | Ham veri aktarımı | AI destekli yorum ve sektörel analiz |
| Yönetici özeti | Yok | Otomatik üretiliyor |

---

## Railway Kurulum Adımları

### 1. Ortam Değişkenlerini Ekle (Variables)

Railway dashboard → Projen → Variables sekmesi:

```
ANTHROPIC_API_KEY   = sk-ant-xxxxxxxxxxxxxxxxxxxx
EMAIL_SENDER        = burak.barantex@gmail.com
EMAIL_APP_PASSWORD  = uavw udfa pztt csvn
EMAIL_RECIPIENTS    = burak.baran@gumussuyu.com.tr,gonca.yuca@gumussuyu.com.tr,...
```

> ⚠️ config.json'daki şifreyi Railway Variables'a taşı.
> config.json'da bırakırsan da çalışır ama Variables daha güvenlidir.

### 2. ANTHROPIC_API_KEY Nereden Alınır?

1. https://console.anthropic.com adresine git
2. "API Keys" → "Create Key"
3. Kopyala → Railway Variables'a yapıştır

### 3. Dosyaları Railway'e Yükle

```bash
# Mevcut projenin üzerine dosyaları kopyala:
bulten.py           ← YENİ (eski ile değiştir)
bulten_scheduler.py ← YENİ (eski ile değiştir)
requirements.txt    ← YENİ (anthropic eklendi)
railway.json        ← Aynı
config.json         ← Aynı (değişiklik yok)
```

### 4. Deploy Et

Railway otomatik deploy yapar. Log'larda şunu görmelisin:

```
🚀 Günlük Bülten Scheduler v2.0 (Railway)
   Hedef: Her gün 09:30 TR saati
   Kaynaklar: Claude AI + ChemOrbis + Fibre2Fashion + Sedat Sezer
```

---

## Test Etmek İçin

Railway konsolunda:
```bash
python bulten.py
```

---

## Sorun Giderme

| Hata | Çözüm |
|---|---|
| `ANTHROPIC_API_KEY bulunamadı` | Railway Variables'a API key ekle |
| `Claude JSON parse hatası` | Normal, fallback moda geçer — log'a bak |
| `E-posta gönderilemedi` | Gmail App Password'ü yenile |
| `RSS okunamadı` | ChemOrbis/Fibre2Fashion erişim sorunu — diğer kaynaklar devam eder |

---

## Maliyet Tahmini

- Claude API (claude-sonnet): ~$0.003 per bülten
- Aylık 22 iş günü: ~$0.07/ay
- Railway: Mevcut planın dahilinde
