import re
from datetime import date, timedelta

import altair as alt
import streamlit as st
import requests
import pandas as pd

st.set_page_config(page_title="EVDS Veri Gösterim", layout="wide")
st.title("EVDS Veri Gösterim")

TEMEL_ADRES = "https://evds3.tcmb.gov.tr/igmevdsms-dis/"

try:
    anahtar = st.secrets["EVDS_KEY"]
except Exception:
    st.error("API anahtarı bulunamadı.")
    st.stop()


# Aynı veriyi tekrar tekrar çekmemek için 24 saat saklıyoruz.
@st.cache_data(ttl=86400)
def liste_getir(yol):
    cevap = requests.get(TEMEL_ADRES + yol, headers={"key": anahtar}, timeout=60)
    cevap.raise_for_status()
    veri = cevap.json()
    if isinstance(veri, dict) and "items" in veri:
        veri = veri["items"]
    return pd.DataFrame(veri)


def temizle(sutun):
    # Kodları düzgün yazıya çevirir (10.0 yerine 10 gibi)
    return pd.to_numeric(sutun, errors="coerce").astype("Int64").astype(str)


def ilk_gun(tarih, frekans_kodu):
    # Dökümanın kuralı: seçilen frekansın ilk gününü başlangıç yap
    if frekans_kodu == 5:  # Aylık
        return tarih.replace(day=1)
    if frekans_kodu == 6:  # 3 Aylık
        return date(tarih.year, ((tarih.month - 1) // 3) * 3 + 1, 1)
    if frekans_kodu == 7:  # 6 Aylık
        return date(tarih.year, 1 if tarih.month <= 6 else 7, 1)
    if frekans_kodu == 8:  # Yıllık
        return date(tarih.year, 1, 1)
    return tarih


def tarih_coz(metin):
    # EVDS'nin gönderdiği tarih yazılarını gerçek tarihe çevirir
    metin = str(metin).strip()
    if re.fullmatch(r"\d{1,2}-\d{1,2}-\d{4}", metin):
        g, a, y = metin.split("-")
        return pd.Timestamp(int(y), int(a), int(g))
    if re.fullmatch(r"\d{4}-\d{1,2}", metin):
        y, a = metin.split("-")
        return pd.Timestamp(int(y), int(a), 1)
    if re.fullmatch(r"\d{4}-Q[1-4]", metin):
        y, ceyrek = metin.split("-Q")
        return pd.Timestamp(int(y), (int(ceyrek) - 1) * 3 + 1, 1)
    if re.fullmatch(r"\d{4}", metin):
        return pd.Timestamp(int(metin), 1, 1)
    return pd.NaT


# --- Listeleri çek ---
try:
    konular = liste_getir("categories/type=json")
    gruplar = liste_getir("datagroups/mode=0&type=json")
except Exception as hata:
    st.error(f"Listeler alınamadı: {hata}")
    st.stop()

konular["CATEGORY_ID"] = temizle(konular["CATEGORY_ID"])
konular["UST_CATEGORY_ID"] = temizle(konular["UST_CATEGORY_ID"])
gruplar["CATEGORY_ID"] = temizle(gruplar["CATEGORY_ID"])

konu_adi = dict(zip(konular["CATEGORY_ID"], konular["TOPIC_TITLE_TR"]))
tum_konu_kodlari = set(konular["CATEGORY_ID"])

# --- 1) Konu seçimi (kademeli) ---
st.subheader("1) Konu seç")

secili_konu = None
derinlik = 0
while derinlik < 10:
    if derinlik == 0:
        # En üstteki konular: üst konusu listede olmayanlar
        cocuklar = konular[~konular["UST_CATEGORY_ID"].isin(tum_konu_kodlari)]
    else:
        cocuklar = konular[konular["UST_CATEGORY_ID"] == secili_konu]

    if cocuklar.empty:
        break

    secenekler = cocuklar.sort_values("TOPIC_TITLE_TR")["CATEGORY_ID"].tolist()
    baslik = "Ana konu" if derinlik == 0 else f"Alt konu ({derinlik})"
    secim = st.selectbox(
        baslik,
        secenekler,
        index=None,
        placeholder="Seçiniz",
        format_func=lambda kod: konu_adi.get(kod, kod),
        key=f"konu_{derinlik}_{secili_konu}",
    )
    if secim is None:
        break
    secili_konu = secim
    derinlik += 1

if secili_konu is None:
    st.info("Başlamak için bir ana konu seç.")
    st.stop()

# --- 2) Veri grubu seçimi ---
st.subheader("2) Veri grubu seç")

secili_gruplar = gruplar[gruplar["CATEGORY_ID"] == secili_konu]
if secili_gruplar.empty:
    st.info("Bu konunun doğrudan veri grubu yok. Bir alt konu seçmeyi dene.")
    st.stop()

grup_adi = {
    satir["DATAGROUP_CODE"]: f'{satir["DATAGROUP_NAME"]} ({satir["FREQUENCY_STR"]})'
    for _, satir in secili_gruplar.iterrows()
}
grup_kodu = st.selectbox(
    "Veri grubu",
    list(grup_adi.keys()),
    index=None,
    placeholder="Seçiniz",
    format_func=lambda kod: grup_adi[kod],
    key=f"grup_{secili_konu}",
)
if grup_kodu is None:
    st.stop()

# --- 3) Seri seçimi ---
st.subheader("3) Seri seç")

try:
    seriler = liste_getir(f"serieList/type=json&code={grup_kodu}")
except Exception as hata:
    st.error(f"Seri listesi alınamadı: {hata}")
    st.stop()

if "SERIE_CODE" not in seriler.columns:
    st.warning("Beklenmeyen sütunlar geldi. Bu listeyi bana yaz:")
    st.write(list(seriler.columns))
    st.stop()

seri_adi = dict(zip(seriler["SERIE_CODE"], seriler["SERIE_NAME"]))
secilen_seriler = st.multiselect(
    "Seri(ler) (birden fazla seçebilirsin)",
    list(seri_adi.keys()),
    format_func=lambda kod: seri_adi.get(kod, kod),
    key=f"seri_{grup_kodu}",
)

if not secilen_seriler:
    st.info("Grafik için en az bir seri seç.")
    st.stop()

# --- 4) Tarih ve görünüm ayarları ---
st.subheader("4) Tarih ve görünüm ayarları")

bugun = date.today()
sol, sag = st.columns(2)
with sol:
    bas = st.date_input(
        "Başlangıç tarihi",
        value=bugun - timedelta(days=365),
        min_value=date(1950, 1, 1),
        max_value=bugun,
        format="DD.MM.YYYY",
    )
with sag:
    bit = st.date_input(
        "Bitiş tarihi",
        value=bugun,
        min_value=date(1950, 1, 1),
        max_value=bugun,
        format="DD.MM.YYYY",
    )

FREKANSLAR = {
    "Serinin kendi frekansı": None,
    "Günlük": 1,
    "İşgünü": 2,
    "Haftalık": 3,
    "Ayda 2 kez": 4,
    "Aylık": 5,
    "3 Aylık": 6,
    "6 Aylık": 7,
    "Yıllık": 8,
}
DONUSUMLER = {
    "Ortalama": "avg",
    "En düşük": "min",
    "En yüksek": "max",
    "Dönem başı değeri": "first",
    "Dönem sonu değeri": "last",
    "Toplam": "sum",
}
FORMULLER = {
    "Düzey (olduğu gibi)": 0,
    "Önceki döneme göre yüzde değişim": 1,
    "Önceki döneme göre fark": 2,
    "Yıllık yüzde değişim": 3,
    "Yıllık fark": 4,
    "Bir önceki yılın sonuna göre yüzde değişim": 5,
    "Bir önceki yılın sonuna göre fark": 6,
    "Hareketli ortalama (1 yıl)": 7,
    "Hareketli toplam (1 yıl)": 8,
}

s1, s2, s3 = st.columns(3)
with s1:
    frekans_adi = st.selectbox("Frekans", list(FREKANSLAR.keys()))
frekans_kodu = FREKANSLAR[frekans_adi]
with s2:
    donusum_adi = st.selectbox(
        "Frekans dönüşüm yöntemi",
        list(DONUSUMLER.keys()),
        disabled=frekans_kodu is None,
        help="Sadece bir frekans seçersen kullanılır.",
    )
with s3:
    formul_adi = st.selectbox("Hesaplama", list(FORMULLER.keys()))

if st.button("Grafiği göster", type="primary"):
    if bas > bit:
        st.error("Başlangıç tarihi bitiş tarihinden sonra olamaz.")
        st.stop()

    bas_ayarli = ilk_gun(bas, frekans_kodu)
    if bas_ayarli != bas:
        st.info(
            f"Seçtiğin frekansın eksiksiz görünmesi için başlangıç tarihi "
            f"{bas_ayarli.strftime('%d.%m.%Y')} olarak ayarlandı."
        )

    adet = len(secilen_seriler)
    yol = (
        f"series={'-'.join(secilen_seriler)}"
        f"&startDate={bas_ayarli.strftime('%d-%m-%Y')}"
        f"&endDate={bit.strftime('%d-%m-%Y')}"
        f"&type=json"
    )
    if frekans_kodu is not None:
        yol += f"&frequency={frekans_kodu}"
        yol += "&aggregationTypes=" + "-".join([DONUSUMLER[donusum_adi]] * adet)
    if FORMULLER[formul_adi] != 0:
        yol += "&formulas=" + "-".join([str(FORMULLER[formul_adi])] * adet)

    try:
        with st.spinner("Veriler EVDS'den alınıyor..."):
            ham = liste_getir(yol)
    except Exception as hata:
        st.error(f"Veri alınamadı: {hata}")
        st.stop()

    if ham.empty or "Tarih" not in ham.columns:
        st.warning("Bu seçim için veri gelmedi. Tarih aralığını veya seçimleri değiştirip tekrar dene.")
        st.stop()

    if len(ham) >= 1000:
        st.warning(
            "1000 gözlem sınırına ulaşıldı. Daha eski veriler gelmemiş olabilir; "
            "tarih aralığını daraltmayı dene."
        )

    # Tabloyu hazırla
    tarihler = ham["Tarih"].apply(tarih_coz)
    sonuc = ham.drop(columns=[c for c in ("Tarih", "UNIXTIME") if c in ham.columns])
    for sutun in sonuc.columns:
        sonuc[sutun] = pd.to_numeric(sonuc[sutun], errors="coerce")

    # Sütun adlarını okunur seri adlarıyla değiştir
    yeni_adlar = {}
    for sutun in sonuc.columns:
        for kod in secilen_seriler:
            if sutun.startswith(kod.replace(".", "_")):
                yeni_adlar[sutun] = seri_adi.get(kod, kod)
                break
    sonuc = sonuc.rename(columns=yeni_adlar)

    tarih_sorunlu = bool(tarihler.isna().any())
    if tarih_sorunlu:
        st.warning("Bazı tarihler tanınamadı; grafik sıra numarasına göre çiziliyor.")
        sonuc.index = ham["Tarih"].astype(str)
    else:
        sonuc.index = tarihler
        sonuc = sonuc.sort_index()

    st.subheader("Grafik")

    if tarih_sorunlu:
        st.line_chart(sonuc)
    else:
        uzun = (
            sonuc.rename_axis("Tarih")
            .reset_index()
            .melt(id_vars="Tarih", var_name="Seri", value_name="Deger")
            .dropna(subset=["Deger"])
        )
        gun_sayisi = (sonuc.index.max() - sonuc.index.min()).days
        uzun_aralik = gun_sayisi > 1825  # 5 yıldan uzun mu?

        eksen = alt.Axis(
            format="%Y" if uzun_aralik else "%m.%Y",
            tickCount="year" if uzun_aralik else 8,
            labelAngle=0,
            labelOverlap=True,
            grid=False,
        )
        x_kodlama = alt.X("Tarih:T", title=None, axis=eksen)

        cizgi = (
            alt.Chart(uzun)
            .mark_line()
            .encode(
                x=x_kodlama,
                y=alt.Y("Deger:Q", title=None, scale=alt.Scale(zero=False)),
                color=alt.Color("Seri:N", legend=alt.Legend(orient="bottom", title=None)),
            )
        )

        # Fareyi yakalamak için görünmez dikey şeritler (her tarihe bir şerit).
        # Üzerine gelince sadece bilgi kutusu (tooltip) çıkar: tarih ve değer(ler).
        genis = sonuc.rename_axis("Tarih").reset_index()
        guvenli_adlar = {}
        ipuclari = [alt.Tooltip("Tarih:T", title="Tarih", format="%d.%m.%Y")]
        seri_sutunlari = [c for c in genis.columns if c != "Tarih"]
        for sira, ad in enumerate(seri_sutunlari):
            guvenli_adlar[ad] = f"s{sira}"
            ipuclari.append(alt.Tooltip(f"s{sira}:Q", title=str(ad), format=",.4f"))
        genis = genis.rename(columns=guvenli_adlar)

        serit_genisligi = max(2, 1200 / max(len(genis), 1))
        serit = (
            alt.Chart(genis)
            .mark_rule(size=serit_genisligi, opacity=0)
            .encode(x=x_kodlama, tooltip=ipuclari)
        )

        grafik = alt.layer(cizgi, serit)
        st.altair_chart(grafik, use_container_width=True)

    with st.expander("Verileri tablo olarak gör"):
        st.dataframe(sonuc)