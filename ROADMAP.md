# cloud-cam-viewer — tahlil va rivojlantirish rejasi

> Har band: **nima · qayerda · nega · qanday**. Bandlar kodga solishtirib tekshirilgan;
> tekshirilgan joylar fayl:satr bilan ko'rsatilgan.
> `[x]` — bajarilgan, `[ ]` — qilinishi kerak.

---

## 0. Umumiy baho

Loyiha oddiy viewer emas — bulut streamini olish, RTP/PS/TS transporti, selective
encryption/decryption, jitter/pacing, FFmpeg izolyatsiyasi, reconnect/watchdog va Python API
qatlamlarining birgalikdagi ishi. Media pipeline va reverse-engineered protokol bilan ishlash —
kuchli. Algoritmik qism zaif emas; asosiy ehtiyoj — vaqt o'tishi bilan **maintain, diagnostika
va ko'p kamera** uchun kerak bo'ladigan infratuzilma qatlamlari.

**To'g'ri strategiya:** ishlayotgan media pipeline'ni qayta yozish EMAS — uning atrofini test,
holat-boshqaruvi, observability, xavfsizlik va scalability bilan kuchaytirish.

### Loyihaning eng katta yashirin xavfi

Yadro `pyezvizapi` ning **ichki** funksiyalarini almashtirish (monkeypatch) ustiga qurilgan —
5 ta nuqtada, versiya cheklovisiz va hech qanday tekshiruvsiz. Bu reja'dagi boshqa hamma
narsadan oldin turadi, chunki upstream bitta relizda loyihani **jimgina** buzishi mumkin
(§A ga qarang). 71 ta test bor, lekin hech qachon avtomatik ishlamaydi (§B).

### Prioritet jadval

| Pr | Masala | Ta'sir | Yo'nalish |
|----|--------|--------|-----------|
| **P0** | **Upstream monkeypatch guardsiz** | **Jim buziladi** | Versiya pin + import-time tekshiruv |
| **P0** | **CI yo'q** | Testlar himoya qilmaydi | GitHub Actions |
| P0 | First-frame latency | Viewer UX | Pre-warm + single-flight + pool + GOP |
| P0 | Aniq kod xatolari (port, zombi, refresh) | 24/7 barqarorlik | Kichik, aniq tuzatishlar |
| P0 | Exception/logging | Yashirin failure, debug qiyin | Structured logging + kontekst |
| P1 | `stream_manager` testsiz | Eng muhim qism qo'riqlanmagan | Reconnect/watchdog testlari |
| P1 | Observability / metrikalar | Regressiyani ko'rish qiyin | Ichki metrics + health |
| P1 | Media test fixturelar | Refactor xavfli | RTP/NAL/PS/TS/encrypted fixturelar |
| P1 | decrypt_proxy responsibility | Maintainability | Bosqichma-bosqich qatlamlarga bo'lish |
| P1 | Process-per-camera scaling | Resurs overhead | Supervisor + bounded concurrency |
| P2 | Secrets-at-rest | Credential xavfi (kontekstga bog'liq) | Fayl ruxsati → keyring |
| P2 | IPC flag-fayllar | Fragile + to'qnashadi | Event-based IPC |
| P2 | MediaMTX fan-out | Ko'p browserga tarqatish | Media qatlamda fan-out |

---

## A. Upstream bog'liqlik (P0 — yangi, eng yuqori)

- [ ] **`pyezvizapi` versiyasi pinlanmagan** — `pyproject.toml` `dependencies`
  - Hozir shunchaki `"pyezvizapi"`. Bu **reverse-engineering kutubxonasi**, ya'ni tez-tez
    o'zgaradi. Yangi o'rnatma istalgan versiyani tortadi.
  - Qanday: `pyezvizapi>=1.0.5,<1.1` kabi oraliq; `requirements.txt` da aniq versiya
    (hozir sinalgani — `1.0.5.0`).

- [ ] **5 ta monkeypatch nuqtasi tekshirilmaydi** — `decrypt_proxy.py:86`, `:786`,
  `vtm_cache.py:154-187`
  - Loyiha quyidagilarni almashtiradi yoki import qiladi:
    `_cs.get_vtm_page_list`, `_cs.get_vtdu_token_v2`, `_cs.build_vtm_url`,
    `open_cloud_stream`, `rtp_payload`, `EzvizClient`.
  - Upstream ularni qayta nomlasa — import vaqtida `AttributeError`. Bu **yaxshi** holat
    (darrov ko'rinadi).
  - Qanday: `cloudcam/compat.py` — import vaqtida shu 6 ta nomning borligini tekshirib,
    yo'q bo'lsa aniq xabar beradigan xato ("pyezvizapi X.Y bilan mos emas").

- [ ] **`build_vtm_url` patch'i JIM buziladi** — `decrypt_proxy.py:766-791`
  - `set_substream` URL'dagi `stream=1` ni regex bilan `stream=2` ga almashtiradi. Upstream
    URL formatini o'zgartirsa (masalan `stream` → `streamType`), regex hech nimani
    o'zgartirmaydi, **xato ham bermaydi** — substream jimgina asosiy oqimga aylanadi va grid
    36 barobar ko'p trafik tortadi.
  - Hozirgi test buni tutmaydi: `test_substream.py` `build_vtm_url` ni **o'zi soxta** qilib
    qo'yadi, ya'ni faqat bizning regex'ni sinaydi, upstream formatini emas.
  - Qanday: `set_substream` almashtirish **muvaffaqiyatli bo'lganini** tekshirsin (natija
    kirishdan farq qilsinmi) va farq qilmasa `WARNING` bersin. Qo'shimcha: upstream'ning
    haqiqiy `build_vtm_url` chiqishiga qarshi bitta test (pyezvizapi o'rnatilgan bo'lsa).

- [ ] **Import vaqtidagi yon ta'sirlar** — `decrypt_proxy.py:86-87`
  - Modulni import qilishning o'zi upstream'ni global almashtiradi va
    `vtm_cache.install()` ni chaqiradi. Boshqa kod `pyezvizapi` ni to'g'ridan-to'g'ri
    ishlatsa, u bilmagan holda bizning patch'imizni oladi. Testlar ham import tartibiga
    bog'liq bo'lib qoladi.
  - Qanday: `install_patches()` funksiyasiga ko'chirib, `serve()`/`list_cameras()` dan
    chaqirish (idempotent).

- [ ] **`requirements.txt` ↔ `pyproject.toml` bir-biriga zid**
  - `requirements.txt` da `opencv-python` **majburiy** dep; `pyproject.toml` da esa u
    ixtiyoriy `[viewer]` extra. "Yadro headless" da'vosi requirements orqali buziladi.
  - Qanday: `requirements.txt` ni `-e .[viewer]` ga qisqartirish yoki pyproject bilan
    moslashtirish (yagona haqiqat manbasi).

---

## B. CI va sifat darvozasi (P0 — yangi)

- [ ] **GitHub Actions yo'q** — `.github/` papkasi mavjud emas
  - 71 ta test bor, lekin push/PR da hech qachon ishlamaydi. Manba matnini qo'riqlaydigan
    testlar (`nobuffer`, featureCode, `_fail_open`) aynan **eslab qolish qiyin** narsalarni
    qo'riqlaydi — ular avtomatik ishlamasa, ma'nosi yarmiga tushadi.
  - Qanday: `.github/workflows/ci.yml` — Python 3.10/3.12, `pip install -e ".[dev]"`,
    `pytest`, `ruff check`. `pyezvizapi` stub bilan ishlagani uchun bulut hisobi shart emas.
- [ ] **Tavsiya:** matritsaga Windows + Linux qo'shish — loyihada Windows'ga xos yo'llar bor
  (`venv/Scripts/python.exe`, `stream_manager.py:95`).
- [ ] `ruff` konfiguratsiyasi bor, lekin hech qachon ishlatilmagan (venv'da o'rnatilmagan).

---

## 1. First-frame latency (P0)

> **Tuzatish:** avvalgi reja'da "PacedWriter `TARGET_S = 1.0` birinchi kadrga ~1s qo'shadi"
> deyilgan edi — **bu noto'g'ri**. `PacedWriter._run` (`decrypt_proxy.py:727-741`) navbatga
> tushgan birinchi kadrni **darhol** yozadi; `TARGET_S` faqat keyingi kadrlar tezligini
> sozlaydi, bufer to'lishini kutmaydi. Bu yo'nalishda vaqt sarflamang.

Birinchi kadrgacha vaqtni haqiqatan quyidagilar belgilaydi:

- [ ] **VTM keshni proaktiv qilish** — `vtm_cache.py`, `client.connect()`
  - Kesh faqat birinchi ochilishda to'ladi, TTL (3600s) dan keyin sovaydi → yana ~6s
    (o'lchangan: issiq 0.24s / sovuq 5.76s). `connect()` dan keyin `pagelist`+`vtdu` ni
    oldindan chaqirish; fonda ~50 daqiqada bir yangilash (stale-while-revalidate).
- [ ] **Single-flight** — `vtm_cache.py`
  - Bir vaqtda ko'p kamera sovuq keshda ochilsa, har biri alohida `pagelist` so'rovini otadi
    (thundering herd). Bitta uchuvchi so'rov qolganlarni kutdirsin.
- [ ] **Shifr-qarori uchun buferlash** — `decrypt_proxy.py:101` `_INTER_SAMPLES = 8`
  - **Yangi band.** "A-body" variantida dekodlovchi inter-slice qarorini chiqarmaguncha
    chiqish navbatda ushlanadi — ya'ni **8 ta inter NAL** kutiladi (~15 fps da ~0.5s).
    Qaror chiqmasa `_INTER_MAX_PENDING` (4 MB) gacha ushlanadi.
  - Qanday: namunalar sonini kamaytirish xavfli (qaror sifati tushadi), lekin *ehtimollik
    yetarli bo'lsa erta chiqish* mumkin: 4 ta namunada farq katta bo'lsa (masalan >0.5)
    qarorni darrov qabul qilish. Avval o'lchang: qaror qancha vaqt oladi.
- [ ] **Proxy pool / prefetch** — `stream_manager.py:98` `_start_proxy`
  - Har ochilish yangi `python -m cloudcam.decrypt_proxy` subprocess'i (interpretator
    ko'tarilishi + `numpy`/`Crypto` importi + bulut ulanishi) ≈ 0.5–2s. Ehtimoli bor
    kameralarni (sevimli/so'nggi/hover) oldindan issiq ushlash.
- [ ] **`_reader_loop` qat'iy kutishlari** — `stream_manager.py:195` (`sleep(0.3)`), `:71`
  - `_wait_port` 0.3s qadam bilan poll qiladi + qo'shimcha `sleep(0.3)` ≈ 0.3–0.6s.
    Poll'ni 0.05s ga tushirish; port tayyor bo'lishi bilan kutishni tugatish.
- [ ] **ffmpeg probe** — `stream_manager.py:164-165`
  - `probesize`/`analyzeduration` = 200 KB. Annex-B kirishda SPS/PPS birinchi kilobaytlarda
    bo'ladi; agar o'lchov ko'rsatsa, 64 KB gacha tushirib sinash mumkin (test allaqachon
    ≤200000 ni qo'riqlaydi — chegarani ham yangilang).
- [ ] **Kamera GOP 1–2s** — Hikvision `Video/Audio → I Frame Interval` (kamera sozlamasi)
  - Bulut birinchi IDR'ni yuborgunicha kutish GOP'ga bog'liq. fps'ga teng yoki 2× → kutish
    1–2s bilan chegaralanadi. **Eng arzon g'alaba, kod o'zgarmaydi.**
- [ ] **Substream'ni default preview** — `settings.py` `stream_mode`
  - Grid/preview `sub`, fullscreen'da `main`. (`auto` fallback allaqachon bor.)
- [ ] **Metrika:** cold/warm start p50/p90/p99 first-frame latency o'lchash. *Avval o'lchang,
  keyin optimallashtiring* — yuqoridagi bandlarning qaysi biri hukmron ekani noma'lum.

---

## 2. Aniq kod xatolari (P0)

- [ ] **Port qayta ishlatilmaydi** — `stream_manager.py:349` `_next_port`, `:355` `remove()`
  - `_next_port` faqat oshadi (`remove()` uni qaytarmaydi); UI'dan ochib-yopishda cheksiz
    o'sadi va oxiri 65535 dan oshadi. Bo'shagan portlar uchun free-list.
- [ ] **Eski subprocess `wait()` qilinmaydi** — `:100`, `:327-330`
  - Kod bo'ylab `terminate()` bor, ammo **birorta** `wait()`/`poll()` yo'q → reaping GC'ga
    tayanadi; ko'p reconnect'da zombi jarayonlar (POSIX'da) va Windows'da ushlangan
    handle'lar. `terminate()` dan keyin qisqa `wait(timeout=…)`, keyin `kill()`.
- [ ] **Port bandligi tekshirilmaydi** — `stream_manager.py:348-349`
  - Eski jarayon portni ushlab tursa, `ThreadingHTTPServer` bind'da yiqiladi va bola-jarayon
    jimgina o'ladi (`stdout`/`stderr` = `DEVNULL`). Bind'dan oldin tekshirish yoki port 0 +
    tanlangan portni ota-jarayonga xabar qilish.
- [ ] **`add()` yangi parametrlarni jimgina e'tiborsiz qoldiradi** — `stream_manager.py:346`
  - `(serial, channel)` bor bo'lsa yangi `key`/`decrypt`/`width` qo'llanmaydi, xato ham
    berilmaydi. Hujjatlash yoki `reopen=True` imkoni.
- [ ] **`refresh_session` har xatoda to'liq login'ga tushadi** — `client.py:240-269`
  - `except Exception: pass` tarmoq blipini ham "sessiya eskirgan" deb qabul qiladi → keraksiz
    to'liq `login()` → CAPTCHA/2FA xavfi (aynan biz qochmoqchi bo'lgan narsa).
  - Qanday: `requests` tarmoq xatolarini (`ConnectionError`, `Timeout`) retry bilan ajratish;
    login faqat `meta.code` sessiya eskirganini aytganda.
- [ ] **`PacedWriter.feed` NAL sarlavhasi chunk chegarasida** — `decrypt_proxy.py:662`
  - `data[hdr + 1] if hdr + 1 < len(data) else 0` — NAL sarlavhasining 2-bayti keyingi
    chunk'ga tushsa, 0 deb o'qiladi. HEVC'da tur 1-baytdan olinadi, shuning uchun hozir
    zarar ko'rinmaydi, lekin bu tasodif. Qoldiqni saqlab, keyingi chunk bilan qo'shib o'qish.
- [ ] **`channel_missing` `have` va `real` nomuvofiqligi** — `vtm_cache.py:143-149`
  - `real` (0-siz to'plam) hisoblanadi va faqat "bo'shmi" tekshiruvi uchun ishlatiladi;
    yakuniy qaror esa `have` bo'yicha. `real ⊆ have` bo'lgani uchun bu **xavfsiz tomonga**
    xato (kamroq "yo'q" deydi), lekin niyat noaniq. Ataylab bo'lsa — izoh; bo'lmasa — `real`.
- [ ] **`_CLIENT_TYPE` import vaqtida muzlatiladi** — `decrypt_proxy.py:45`
  - `get_active().platform` modul import qilinganda o'qiladi. Bir jarayonda `set_active()`
    bilan platforma o'zgartirilsa, `clientType` eski qoladi. Bola-jarayonda muammo yo'q
    (u yangi boshlanadi), lekin kutubxona rejimida bor.

---

## 3. Exception va structured logging (P0)

- [ ] **`except Exception` ni toraytirish** — 17 ta joy (`cloudcam/` ichida)
  - Ayniqsa: `client.py:113` (login backoff hisoblagichi), `client.py:181` (`load_token`),
    `stream_manager.py:268` (`_reader_loop` ning butun tanasi!), `decrypt_proxy.py:743`
    (`PacedWriter._run` yozuvi), `api.py:223` (kalit olish).
  - `stream_manager.py:268` eng xavflisi: reader loop'dagi **har qanday** dasturiy xato
    (masalan `numpy` reshape) "uzildi, qayta ulanamiz" deb talqin qilinadi va cheksiz
    takrorlanadi — sabab hech qayerda ko'rinmaydi.
  - Qanday: kutilgan exceptionlarni alohida tutish; umumiy `Exception` faqat boundary
    qatlamida va **majburiy `logger.exception()`** bilan.
- [ ] **Structured logger** — `logging` moduli
  - Har logda: timestamp, serial, channel, stream state, pid, level. Kutubxona ichida `print`
    o'rniga `logging` (hozir `stream_manager.py:391,393` token loop'da `print` qiladi —
    kutubxona uchun noto'g'ri). CLI/`app.py` da `print` qolishi normal.
- [ ] **Bola-jarayon chiqishi butunlay yo'qoladi** — `stream_manager.py:139`
  - `stdout=DEVNULL, stderr=DEVNULL`. Proxy yiqilsa sabab **hech qayerda** qolmaydi.
  - Qanday: faylga yo'naltirish yoki pipe orqali o'qib, ota-jarayon logiga qo'shish
    (kamida `--debug` rejimida).
- [ ] **Sensitive maskalash** — email, token, verification code, session logga chiqmasin.
- [ ] **Stream lifecycle eventlari:** STARTING → CONNECTING → BUFFERING → PLAYING → STALLED →
  RECONNECTING → STOPPED.

---

## 4. Observability / metrikalar (P1)

- [ ] Ichki metrics modelini standartlashtirish (keyin Prometheus/OpenTelemetry).
- [ ] O'lchamlar: active cameras; connecting/playing/stalled/offline soni; first-frame latency;
  reconnect count/duration; dropped/reordered RTP; jitter buffer depth (`PacedWriter.dropped`
  allaqachon bor); decoder errors; ffmpeg restart count; bytes in/out; CPU/RAM/GPU.
- [ ] Per-camera health endpoint yoki status snapshot (hozirgi `status()` ustiga agregatsiya).
- [ ] **Yangi:** `PacedWriter.dropped` va `CameraStream.reconnect_count` hozir hech qayerda
  ko'rsatilmaydi — arzon boshlanish nuqtasi.

---

## 5. Media test fixturelar (P1 — refactordan OLDIN)

- [ ] Fixturelar: RTP paket; H.264/HEVC NAL; encrypted va clear stream; MPEG-PS/TS.
  *(Qisman bor: `test_inter_encryption.py`, `test_h264_codec_choice.py`, `test_stream_mode.py`
  sintetik oqim quradi — shu yondashuvni umumiy `tests/fixtures/` ga chiqarish.)*
- [ ] Holat testlari: packet loss / reorder / duplicate / corrupt; jitter/pacing; ffmpeg
  crash/restart; disconnect/reconnect; key-error/offline/auth-error.
- [ ] Regression: ma'lum stream fixture → kutilgan decoded frame/metrika.
- [ ] **MPEG-PS yo'li deyarli test qilinmagan** — `PsStreamDecryptor` uchun birorta ham
  maxsus test yo'q, holbuki u eng murakkab demux mantig'iga ega (`_demux`, `_decide`).

---

## 6. `stream_manager.py` — testsiz eng muhim qism (P1 — yangi)

- [ ] **Hozir 0 ta test.** 24/7 ishlashning butun mantig'i shu yerda va u umuman
  qo'riqlanmagan: reconnect backoff, offline backoff (`15 * min(tries, 4)`), freeze
  watchdog (`FREEZE_TIMEOUT`), GPU→software fallback, bo'sh kanal aniqlash
  (`_consec_empty >= 3`).
- [ ] Qanday: `subprocess.Popen` va `ffmpeg` ni soxtalashtirib (`monkeypatch`), `_reader_loop`
  ni sintetik kadr oqimi bilan haydash. `test_stream_mode.py` dagi yondashuv (haqiqiy
  serverni soxta oqim bilan ishga tushirish) shu yerda ham ishlaydi.
- [ ] Eng muhim qo'riqlanishi kerak bo'lgan xatti-harakat: **kadr kelganda `error` tozalanadi,
  kelmaganda backoff o'sadi** — regressiya bo'lsa 200 kamerali hisob bulutni uradi.

---

## 7. decrypt_proxy.py — bosqichma-bosqich refactor (P1)

Fayl ~1080 satr va 6 ta mas'uliyatni birlashtiradi. Ishlaydigan faylni birdan bo'lmaslik;
testlar bilan ajratish:

- [ ] `protocol/rtp.py` — RTP parsing, FU/AP yig'ish
- [ ] `protocol/mpeg_ps.py`, `protocol/mpeg_ts.py`
- [ ] `codec/h264.py`, `codec/hevc.py` — NAL turlari, param-set markerlari
- [ ] `crypto/aes.py`, `crypto/detector.py` — AES + shifr varianti + inter detektori
- [ ] `stream/jitter.py` (PacedWriter), `stream/proxy.py` (HTTP + orkestratsiya)
- [ ] `compat.py` — upstream patch'lari bir joyda (§A)
- *Maqsad:* har qatlamni mustaqil test qilish; protokol o'zgarishlari boshqa kodga kam ta'sir
  qilsin.

---

## 8. "Testsiz tegmaslik" tamoyili (qoida)

Quyidagilarni **regression fixture/testsiz** qayta yozmaslik:

- Selective encryption detection algoritmi
- RTP/NAL parsing behavior
- Jitter/pacing algoritmi
- VTM cache invalidation behavior
- FFmpeg process lifecycle va bayroqlari
- Cloud protokol request/response ketma-ketligi

> Qoida: **avval regression fixture → keyin refactor.**

---

## 9. Process/thread modeli va scalability (P1)

- [ ] **Supervisor qatlami** — processlarni nazorat, per-camera resource limit + timeout,
  crash-loop'da exponential backoff (asosan bor, kuchaytirish).
- [ ] **Bounded concurrency / worker pool** — kamera soni oshganda.
- [ ] **Idle kameralarni suspend** qilish (grid'da fullscreen'ga o'tilganda qolganlari).
- [ ] **Xotira:** har kamera `latest_frame` sifatida xom BGR kadr ushlaydi —
  1280×720×3 ≈ **2.7 MB/kamera**. 100 kamera ≈ 270 MB faqat oxirgi kadrlar uchun
  (`get_frame()` yana `.copy()` qiladi). Preview uchun kichikroq o'lcham yoki JPEG saqlash.
- [ ] **Benchmark** (taxmin emas): 10/50/100/500 kamera load test; preview-only va full-res
  alohida; CPU/RAM/network/process/FD; bir kamera resurs budjeti; backpressure.
- [ ] **Halol chegara:** hozirgi model (har kamera 2 subprocess + xom BGR kadr) "bir vaqtda N
  ko'rish" uchun ajoyib, "1000–5000 concurrent decode/record" uchun emas. 5000 kamerani doim
  decode qilish ≠ 5000 kameradan kerak bo'lganda ochish — arxitektura workloadga qarab
  tanlanadi.

---

## 10. MediaMTX / fan-out integratsiyasi (P2)

- [ ] Core viewerga bog'lanib qolmasin; adapter sifatida:
  `cloud-cam core → MediaMTX → WebRTC/HLS → browser`, shuningdek AI consumer va recorder
  adapterlari.
- [ ] **Muhim:** ko'p browserga bitta streamni tarqatishda har browser uchun bulutdan alohida
  upstream OCHMASLIK — fan-out media qatlamda ("bir marta ol, ko'pchilikka ber"). Bulutning
  o'zi ham bir vaqtdagi oqim sonini cheklaydi.

---

## 11. Xavfsizlik (P2 — kontekstga qarab)

- [x] Parol bola-jarayonga uzatilmaydi (`settings.py` `_ENV_EXCLUDE`); sirlar `.gitignore`da;
  proxy `127.0.0.1` ga bog'lanadi; token `feature_code` ga bog'langan.
- [ ] `cam_keys.json` / `token.json` plain-text — **fayl ruxsatlarini** qattiqlash (0600).
  Bir foydalanuvchili self-hosted'da ko'pincha yetarli.
- [ ] **Vaqtinchalik bayroq fayllari to'qnashadi** — `decrypt_proxy.py:116-123`
  - `%TEMP%/ezviz_keyerr_<serial>_<ch>.flag` — **umumiy** temp papkada, foydalanuvchi yoki
    hisob ajratmasdan. Bitta hostda ikki hisob bir xil seriyani ochsa, bir-birining
    bayrog'ini o'qiydi/o'chiradi. Ko'p foydalanuvchili hostda begona foydalanuvchi ham
    yozishi mumkin.
  - Qanday: hisob kalitini (`vtm_cache.account_key()`) nomga qo'shish; imkon bo'lsa
    §12 dagi IPC bilan butunlay almashtirish.
- [ ] *Agar kerak bo'lsa:* OS keyring / Windows DPAPI. Self-hosted single-user'da bu P1–2 —
  muhimroq ishlarni kechiktirmasin.
- [ ] Loglarda secret maskalash; key rotation / logout-revoke.
- [ ] Lokal proxy HTTP'da auth yo'q — ko'p foydalanuvchili hostda localhost'dagi har kim
  oqimni o'qiy oladi (single-user'da muammo emas). Tuzatish arzon: tasodifiy token'ni URL
  yo'liga qo'shish.
- [ ] Provider ToS masalasini alohida ko'rish (README'da ogohlantirish bor).

---

## 12. IPC — bayroq fayllardan eventlarga (P2)

- [ ] Hozir bola→ota aloqasi: temp papkadagi bo'sh fayllar (`keyerr`, `offline`). Kamchiliklari:
  to'qnashuv (§11), poliling, holat "yopishib" qolishi, sabab matni yo'q.
- [ ] Qanday: bola-jarayonning `stdout` iga bir qatorli JSON eventlar
  (`{"event":"KEY_ERROR","serial":…}`) — bu bir vaqtning o'zida §3 dagi "bola chiqishi
  yo'qoladi" muammosini ham hal qiladi. Eventlar: `KEY_ERROR`, `OFFLINE`, `AUTH_ERROR`,
  `STALLED`, `DECODER_ERROR`, `FIRST_FRAME`.

---

## 13. Public API va kod sifati (P2)

- [ ] **`Stream` uchun context manager** — `api.py:43`
  - `CloudCam` da `__enter__`/`__exit__` bor, `Stream` da **yo'q**. `with cam.open(...) as s:`
    ishlamaydi, holbuki aynan shu yerda `close()` unutilishi oson.
- [ ] Typed return + custom domain exceptionlar; provider xatolarini umumiy exceptionlarga
  mapping (hozir ko'pi `RuntimeError`).
- [ ] **`PsStreamDecryptor` da inter-shifr aniqlash yo'q** — `decrypt_proxy.py:462`
  - RTP yo'lidagi asosiy yutuq (selektiv shifrni oqimdan aniqlash) PS yo'lida yo'q:
    `_write_nal` har NAL body'ni deshifrlaydi. PS bilan uzatuvchi kamera selektiv shifrlasa,
    README tasvirlagan P-freym buzilishi qaytadi. Hozir README'da cheklov sifatida yozilgan —
    keyingi qadam: `_sample_inter` ni PS yo'liga ham ulash.
- [ ] **`CloudClient.get_devices()` sahifalanmaydi va o'lik kod** — `client.py:276`
  - `limit=50, offset=0` — faqat 1-sahifa. Hech qayerdan chaqirilmaydi
    (`decrypt_proxy._device_names` o'zining sahifalangan nusxasini ishlatadi).
  - Qanday: o'chirish yoki `_device_names` bilan birlashtirish (takror sessiya-qurish kodi
    `decrypt_proxy.py:47` va `:815` da ikki marta).
- [ ] `app.py`/`single.py` eskirgan (`import config`; `make_grid` `viewer._grid` bilan deyarli
  aynan takror) — CLI/api ustidagi ingichka o'ramga aylantirish yoki olib tashlash.
- [ ] `PacedWriter._nal_kind(b0, b1)` — `b1` hech qachon ishlatilmaydi.
- [ ] Magic number'lar constants'da; provider string/endpointlar bir joyda; reverse-engineered
  fieldlarga izoh + fixture (asosan bor — izoh sifati loyihaning kuchli tomoni).

---

## 14. Refactor tartibi (yo'l xaritasi)

Tartib ataylab shunday: **avval himoya to'ri, keyin o'zgarish.**

1. **§A upstream guard + versiya pin** — bir kunlik ish, eng katta xavfni yopadi
2. **§B CI** — mavjud 71 test ishlay boshlaydi
3. Logging + error handling (behavior o'zgarmaydi)
4. §2 dagi aniq xatolar (port, zombi, refresh) — kichik, izolyatsiyalangan
5. Health/state model (lifecycle holatlari)
6. §6 `stream_manager` testlari + §5 media fixturelar
7. `decrypt_proxy`ning eng mustaqil qismlarini ajratish
8. Flag-fayllardan event-based IPC
9. Secrets storage
10. First-frame: **avval o'lchash**, keyin §1 dagi bandlar
11. 10–500+ kamera benchmark
12. Natijaga qarab worker pool / scalability modeli
13. MediaMTX/WebRTC/API integratsiyasi

> §1 (latency) va §2 (aniq xatolar) xavfsiz va izolyatsiyalangan — logging bilan parallel
> boshlash mumkin. Ammo §A va §B ulardan oldin: ularsiz har qanday o'zgarish qo'riqlanmagan.

---

## 15. Allaqachon bajarilgan (QAYTA QILMANG)

- [x] Session reuse (token → refresh → login) — `client.connect()`
- [x] Yoshga qarab refresh + login backoff (60s→30min)
- [x] MAC'dan per-install featureCode + faylga saqlash
- [x] Bulutdan tasdiqlash kodi + bir martalik 2FA elevation
- [x] VTM hisob-darajali kesh, atomar yozuv, ehtiyotkor invalidatsiya (provider error'da
  avto-o'chirmaydi)
- [x] Sozlamaning to'liq bola-jarayonga uzatilishi (`Settings.to_env`), paroldan tashqari
- [x] FFmpeg flaglari asosli (`nobuffer` yo'q → birinchi IDR; kichik probe; passthrough)
- [x] PacedWriter (burst + backpressure, kalit-kadr chegarasida drop)
- [x] auto substream → main fallback
- [x] `channel_missing` darvozasi (NVR'da yo'q kanalda 20s kutmaydi)
- [x] Watchdog qotgan ffmpeg'ni o'ldiradi
- [x] OpenCV core'dan ajratilgan (kech import, ixtiyoriy dep)
- [x] `CloudCam` context manager
- [x] Nozik joylarga testlar (identity / keys / vtm_cache / paced_writer / substream /
  session_reuse / stream_mode / h264_codec_choice / ffmpeg_flags / inter_encryption)
