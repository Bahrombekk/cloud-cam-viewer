// SPDX-License-Identifier: MIT
// isup-bridge: Hikvision ISUP 5.0 to RTSP bridge, controlled by cloudcam.
//
// The device registers with this process over ISUP (it dials out, so no port has to be
// opened on the camera side). Its live stream (MPEG-PS) is published WITHOUT
// transcoding (-c copy) to an RTSP server such as MediaMTX:
//   main stream  -> <RTSP_BASE>/isup/<DeviceID>/<channel>
//   substream    -> <RTSP_BASE>/isup/<DeviceID>/<channel>/sub
//
// Which streams are open is decided by the controller (cloudcam's IsupBridge, or any
// other client) through PUT /want. Until a want list has been received the bridge runs
// in standalone mode: every connected device's CHANNELS are opened with STREAM_TYPE.
//
// Control API (API_PORT; keep it on a private network):
//   GET /health   -> {"ok":true}                        (no token required)
//   GET /devices  -> connected devices, channels, open streams, rejected devices (JSON)
//   PUT /want     -> text, one line per stream: "<DeviceID> <channel> <0|1>" (0 main, 1 sub)
//   PUT /keys     -> text, one line each: "dev <ID> <KEY>", "block <ID>",
//                    "prefix <PREFIX> <KEY>", "default on|off"   (see apply_keys)
// When API_TOKEN is set every request except /health needs the X-Bridge-Token header.
//
// Environment:
//   PUBLIC_IP        address the devices can reach this host on (e.g. 203.0.113.10); required
//   ISUP_KEY         fallback Encryption Key (8-32 letters/digits) for devices without
//                    their own key; optional when keys are sent with PUT /keys
//   CMS_PORT         registration port (17660, tcp+udp)
//   STREAM_PORT      video port (17661, tcp)
//   API_PORT         control API (8091)
//   API_TOKEN        optional shared secret for the control API
//   RTSP_BASE        where to publish, e.g. rtsp://mediamtx:8554 or rtsp://user:pass@host:8554
//   MTX_API          optional MediaMTX API URL; only for a mediamtx.yml that does not allow
//                    publishing to isup/ paths (the path is then created through the API)
//   CHANNELS         standalone mode: channels, e.g. "1" or "1,2,3,4"
//   STREAM_TYPE      standalone mode: 0 main stream, 1 substream
//   ACCESS_SECURITY  SDK access mode: 0 compatible (default), 2 secure (ISUP 4.0+ only)
//   SDK_LIB          directory with the SDK libraries (/opt/isup/lib)
//   SDK_LOG_LEVEL    SDK log level written to /opt/isup/logs (2)
//
// Log messages and inline comments are in Uzbek; the README explains the key log lines.
#include <arpa/inet.h>
#include <netinet/in.h>
#include <signal.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <sstream>
#include <string>
#include <thread>
#include <tuple>
#include <vector>

#include "HCISUPCMS.h"
#include "HCISUPStream.h"

static std::string env(const char* k, const char* d) {
    const char* v = getenv(k);
    return (v && *v) ? v : d;
}

static std::string g_key, g_public_ip, g_rtsp_base, g_mtx_api, g_api_token;
static int g_cms_port, g_stream_port, g_api_port, g_stream_type;
static std::vector<int> g_channels;

static double now_s() {
    using namespace std::chrono;
    return duration<double>(system_clock::now().time_since_epoch()).count();
}

#define LOG(fmt, ...) do { \
    time_t _t = time(nullptr); char _b[16]; strftime(_b, sizeof _b, "%H:%M:%S", localtime(&_t)); \
    fprintf(stderr, "%s " fmt "\n", _b, ##__VA_ARGS__); fflush(stderr); } while (0)

// Device ID qurilmadan keladi (ishonchsiz) va MediaMTX path nomiga, shell buyrug'iga
// tushadi — shuning uchun faqat xavfsiz belgilar. Boshqasi bo'lsa qurilma ro'yxatda
// ko'rinadi, lekin uning oqimi ochilmaydi.
static bool valid_id(const std::string& s) {
    if (s.empty() || s.size() > 64) return false;
    for (char c : s)
        if (!(isalnum((unsigned char)c) || c == '_' || c == '-')) return false;
    return true;
}

// SDK maydonlari doim ham NUL bilan tugamaydi (masalan byDevProtocolVersion[6]).
static std::string bstr(const void* p, size_t n) {
    const char* c = static_cast<const char*>(p);
    return std::string(c, strnlen(c, n));
}

static std::string json_str(const std::string& s) {
    std::string o = "\"";
    for (unsigned char c : s) {
        if (c == '"' || c == '\\') { o += '\\'; o += (char)c; }
        // < 0x20 va >= 0x7f: qurilmadan kelgan (ishonchsiz) baytlar JSON'ni noto'g'ri UTF-8
        // qilib qo'ymasin — aks holda backend /devices'ni o'qiy olmay ISUP holati qotardi.
        else if (c < 0x20 || c >= 0x7f) { char b[8]; snprintf(b, sizeof b, "\\u%04x", c); o += b; }
        else o += (char)c;
    }
    return o + "\"";
}

// Logga yoziladigan ishonchsiz matn (Device ID, serial): yangi qator / boshqaruv belgisi
// bilan soxta log qatori yasab bo'lmasin.
static std::string log_safe(const std::string& s) {
    std::string o;
    for (unsigned char c : s.substr(0, 64)) o += (c >= 0x20 && c < 0x7f) ? (char)c : '?';
    return o.empty() ? "?" : o;
}

// ---------------------------------------------------------------------------
// ffmpeg'ga yozuvchi: SDK callback'ini HECH QACHON bloklamaymiz (cloudcam PacedWriter
// saboqi — pipe to'lsa qurilma soketi o'qilmay qoladi va kadrlar buziladi).
// Navbat to'lsa eski ma'lumot tashlanadi (keyingi PS pack'dan davom etadi).
struct Session {
    std::string dev;
    int ch = 0;
    int type = 0;  // 0 asosiy, 1 sub
    LONG user = -1;
    LONG cms_session = -1;
    std::atomic<LONG> preview_handle{-1};   // SDK oqimi va nazoratchi turli thread'larda
    FILE* ff = nullptr;
    std::mutex mu;
    std::condition_variable cv;
    std::deque<std::string> q;
    size_t qbytes = 0;
    std::atomic<bool> stop{false};
    std::atomic<bool> opened{false};  // StartPushRealStream qaytdi
    std::atomic<double> last_data{0};
    std::atomic<long long> bytes{0};
    double t_request = 0;
    std::atomic<bool> first_logged{false};
    std::thread writer;

    std::string key() const { return dev + "/" + std::to_string(ch) + "/" + std::to_string(type); }
    std::string path() const {
        return "isup/" + dev + "/" + std::to_string(ch) + (type == 1 ? "/sub" : "");
    }

    bool start_ffmpeg() {
        // Path mediamtx.yml'dagi `~^isup/` dan olinadi. MTX_API faqat eski (regex
        // path'siz) mediamtx.yml uchun — u holda path API orqali yaratiladi.
        if (!g_mtx_api.empty()) {
            std::string cmd = "curl -s -o /dev/null -m 3 -X POST -H 'Content-Type: application/json' -d '{}' "
                              + g_mtx_api + "/v3/config/paths/add/" + path();
            if (system(cmd.c_str()) != 0) LOG("[%s] MediaMTX path yaratishda xato (davom etamiz)", path().c_str());
        }
        // DIQQAT: -fflags nobuffer YO'Q (birinchi IDR yutilib ketadi; cloudcam docs/internals.md).
        // env -u LD_LIBRARY_PATH: SDK eski OpenSSL/zlib olib keladi, ffmpeg ularda yiqiladi.
        std::string ff_cmd =
            "exec env -u LD_LIBRARY_PATH ffmpeg -hide_banner -loglevel warning -nostdin"
            " -fflags +genpts+discardcorrupt -probesize 1000000 -analyzeduration 1000000"
            " -f mpeg -i pipe:0 -map 0:v:0 -c:v copy -an"
            " -f rtsp -rtsp_transport tcp " + g_rtsp_base + "/" + path();
        ff = popen(ff_cmd.c_str(), "w");
        if (!ff) { LOG("[%s] ffmpeg ishga tushmadi", path().c_str()); return false; }
        writer = std::thread([this] { run_writer(); });
        return true;
    }

    void push(const void* data, size_t n) {
        std::lock_guard<std::mutex> lk(mu);
        const size_t cap = 8 * 1024 * 1024;
        while (qbytes + n > cap && !q.empty()) { qbytes -= q.front().size(); q.pop_front(); }
        q.emplace_back(static_cast<const char*>(data), n);
        qbytes += n;
        cv.notify_one();
    }

    void run_writer() {
        while (!stop) {
            std::string chunk;
            {
                std::unique_lock<std::mutex> lk(mu);
                cv.wait_for(lk, std::chrono::milliseconds(500), [this] { return !q.empty() || stop; });
                if (q.empty()) continue;
                chunk.swap(q.front()); q.pop_front(); qbytes -= chunk.size();
            }
            if (fwrite(chunk.data(), 1, chunk.size(), ff) != chunk.size()) {
                LOG("[%s] ffmpeg pipe yopildi", path().c_str());
                stop = true;
                break;
            }
            fflush(ff);
        }
    }

    void close_all() {
        stop = true;
        cv.notify_all();
        if (writer.joinable()) writer.join();
        if (ff) { pclose(ff); ff = nullptr; }
    }
};

struct ChanInfo { int id = 0; std::string name; bool online = true; };
struct DevInfo {
    LONG user = -1;
    std::string serial, full_serial, ip, firmware, protocol, key_source;
    double since = 0;
    // Qurilma ma'lumoti (NVR kanallari) — ulangandan keyin alohida oqimda so'raladi.
    std::string info = "pending";   // pending | ok | failed
    int dev_type = 0, ch_analog = 0, ch_total = 0, start_ch = 1;
    std::vector<ChanInfo> ip_channels;   // ISAPI InputProxy (NVR IP kanallari)
};

using WantKey = std::tuple<std::string, int, int>;  // dev, kanal, tur

static std::mutex g_mu;
static std::condition_variable g_wake;                               // PUT /want -> nazoratchini uyg'otish
static std::map<std::string, DevInfo> g_devices;                     // deviceID -> ma'lumot
static std::map<std::string, std::shared_ptr<Session>> g_sessions;   // key -> session
static std::map<LONG, std::shared_ptr<Session>> g_by_cms_session;    // CMS sessionID -> session
static std::set<std::string> g_starting;                             // ochilayotgan key'lar
static std::set<WantKey> g_want;
static bool g_want_set = false;                                      // backend hech bo'lmasa bir marta yubordimi
static double g_want_at = 0;
static std::map<std::string, int> g_fails;                           // key -> ketma-ket muvaffaqiyatsizlik
static std::map<std::string, double> g_next_try;                     // key -> qachon qayta urinish

// --- Kalitlar (PUT /keys, backend yuboradi) -----------------------------------
// Ustunlik: bloklangan -> qurilma kaliti -> eng uzun prefiks -> zaxira ISUP_KEY -> rad.
// Qurilma ulanayotganda BITTA kalit qaytariladi (variantlarni sinab bo'lmaydi), shuning
// uchun kalit Device ID'dan aniq topilishi shart. Faqat xotirada turadi.
// AuthUse — ENUM_DEV_AUTH (kalit tekshiruvi) natijasi. U ENUM_DEV_ON (qurilma "ulandi")
// uchun BIR MARTALIK ruxsatnoma: `consumed` bo'lgach boshqa ulanish undan foydalana
// olmaydi, `auth_user` esa SDK sessiya handle'i — ma'lum bo'lsa, ON aynan shu
// ulanishdan kelishi shart. Ilgari ruxsatnoma ID + 120 s oynaga bog'langan edi:
// haqiqiy qurilma AUTH'dan o'tgach, ID'ni bilgan hujumchi moslik rejimida kalitsiz
// eski protokol bilan shu ID'ni egallab, qurilmani chiqarib yuborishi mumkin edi
// (xavfsizlik tekshiruvida topilgan).
struct AuthUse {
    std::string key, source;
    double at = 0;
    LONG auth_user = -1;   // AUTH kelgan sessiya (SDK bermasa -1)
    LONG on_user = -1;     // ruxsatnomani ishlatgan (ON) sessiya
    bool consumed = false;
};
struct Rejected { std::string reason; double at = 0; int count = 0; };
static std::map<std::string, std::string> g_dev_keys;                // deviceID -> kalit
static std::set<std::string> g_blocked;
static std::vector<std::pair<std::string, std::string>> g_prefixes;  // (prefiks, kalit), uzuni oldin
static bool g_keys_set = false;
static bool g_allow_default = true;
static std::map<std::string, AuthUse> g_auth_used;                   // deviceID -> qaysi kalit bilan kirdi
static std::map<std::string, Rejected> g_rejected;                   // rad etilgan urinishlar (GET /devices uchun)

static bool valid_key(const std::string& k) {
    if (k.size() < 8 || k.size() > 32) return false;
    for (char c : k) if (!isalnum((unsigned char)c)) return false;
    return true;
}

// g_mu ostida chaqiriladi. Bo'sh qaytsa — rad etiladi.
static AuthUse key_for_locked(const std::string& dev) {
    if (g_blocked.count(dev)) return {"", "blocked"};
    auto it = g_dev_keys.find(dev);
    if (it != g_dev_keys.end()) return {it->second, "device"};
    for (auto& [p, k] : g_prefixes)
        if (dev.compare(0, p.size(), p) == 0) return {k, "prefix:" + p};
    if (g_allow_default && !g_key.empty()) return {g_key, "default"};
    return {"", "unknown"};
}

static void note_rejected_locked(const std::string& dev, const std::string& reason) {
    // Faqat yaroqli Device ID — tasodifiy/yovuz baytlar /devices ro'yxatiga tushmasin.
    if (!valid_id(dev)) return;
    if (g_rejected.size() > 500) g_rejected.clear();  // tasodifiy skanerlar xotirani to'ldirmasin
    auto& r = g_rejected[dev];
    r.reason = reason;
    r.at = now_s();
    r.count++;
}

// Ro'yxat callback'ining pOutBuffer'idan Device ID (NET_EHOME_DEV_REG_INFO va _V12
// ikkalasi ham shu maydon bilan boshlanadi). Uzunlik yetmasa — bo'sh.
static std::string dev_from_out(const void* out, DWORD out_len) {
    if (!out || out_len < offsetof(NET_EHOME_DEV_REG_INFO, byDeviceID) + MAX_DEVICE_ID_LEN) return "";
    auto* r = static_cast<const NET_EHOME_DEV_REG_INFO*>(out);
    return bstr(r->byDeviceID, sizeof r->byDeviceID);
}

static void CALLBACK on_data(LONG, NET_EHOME_PREVIEW_CB_MSG* m, void* user) {
    auto* s = static_cast<Session*>(user);
    if (!m || !s || s->stop) return;   // to'xtatilgan sessiya ([[stop_session]]) — yozmaymiz
    if (m->byDataType == 1) return;  // 40 baytli Hik "IMKH" sarlavha — ffmpeg'ga kerak emas
    if (!s->first_logged.exchange(true))
        LOG("[%s] [vaqt] birinchi video paket: so'rovdan %.2fs", s->path().c_str(), now_s() - s->t_request);
    s->last_data = now_s();
    s->bytes += m->dwDataLen;
    s->push(m->pRecvdata, m->dwDataLen);
}

static BOOL CALLBACK on_newlink(LONG link, NET_EHOME_NEWLINK_CB_MSG* msg, void*) {
    std::shared_ptr<Session> s;
    {
        std::lock_guard<std::mutex> lk(g_mu);
        auto it = g_by_cms_session.find(msg->iSessionID);
        if (it != g_by_cms_session.end()) s = it->second;
    }
    if (!s) { LOG("[stream] noma'lum sessiya %d — rad etildi", msg->iSessionID); return FALSE; }
    LOG("[%s] video ulanish (sessiya %d)", s->path().c_str(), msg->iSessionID);
    s->preview_handle = link;
    NET_EHOME_PREVIEW_DATA_CB_PARAM cb{};
    cb.fnPreviewDataCB = on_data;
    cb.pUserData = s.get();
    cb.byStreamFormat = 0;  // PS
    if (!NET_ESTREAM_SetPreviewDataCB(link, &cb)) {
        LOG("[stream] SetPreviewDataCB xato: %u", NET_ESTREAM_GetLastError());
        return FALSE;
    }
    // Shu orada sessiya to'xtatilgan bo'lsa (nazoratchi preview_handle'ni hali ko'rmagan
    // edi) — oqimni o'zimiz yopamiz, aks holda callback yopilgan sessiyaga yozib turardi.
    if (s->stop) {
        NET_ESTREAM_StopPreview(link);
        return FALSE;
    }
    return TRUE;
}

// To'xtatilgan sessiyalar darhol o'chirilmaydi: SDK callback'iga xom ko'rsatkich
// berilgan, kechikkan callback bo'shatilgan xotiraga yozmasin (use-after-free) — 30 s
// "nafaqa" ro'yxatida turadi, keyin nazoratchi tozalaydi.
static std::vector<std::pair<double, std::shared_ptr<Session>>> g_retired;

static void stop_session(const std::shared_ptr<Session>& s) {
    if (s->preview_handle >= 0) NET_ESTREAM_StopPreview(s->preview_handle);
    if (s->cms_session >= 0) NET_ECMS_StopGetRealStream(s->user, s->cms_session);
    {
        std::lock_guard<std::mutex> lk(g_mu);
        g_by_cms_session.erase(s->cms_session);
        g_retired.emplace_back(now_s(), s);
    }
    s->close_all();
}

// Oqimni ochadi. StartPushRealStream SINXRON va sekin (~5 s — video esa undan oldin
// oqib boshlaydi), shuning uchun har ochilish alohida oqimda: 100 kamerani ketma-ket
// ochish 8 daqiqa bo'lardi.
static void start_session(std::string dev, LONG user, int ch, int type) {
    auto s = std::make_shared<Session>();
    s->dev = dev; s->ch = ch; s->type = type; s->user = user;
    s->t_request = now_s();
    const std::string key = s->key();
    auto done = [&key] { std::lock_guard<std::mutex> lk(g_mu); g_starting.erase(key); };

    if (!s->start_ffmpeg()) { done(); return; }

    NET_EHOME_PREVIEWINFO_IN_V11 in{};
    in.iChannel = ch;
    in.dwStreamType = type;
    in.dwLinkMode = 0;  // TCP
    snprintf(in.struStreamSever.szIP, sizeof in.struStreamSever.szIP, "%s", g_public_ip.c_str());
    in.struStreamSever.wPort = (WORD)g_stream_port;
    NET_EHOME_PREVIEWINFO_OUT out{};
    if (!NET_ECMS_StartGetRealStreamV11(user, &in, &out)) {
        LOG("[%s] StartGetRealStreamV11 xato: %u", s->path().c_str(), NET_ECMS_GetLastError());
        s->close_all();
        std::lock_guard<std::mutex> lk(g_mu);
        g_starting.erase(key);
        g_sessions[key] = s;  // stop=true -> nazoratchi muvaffaqiyatsiz deb hisoblaydi
        return;
    }
    s->cms_session = out.lSessionID;
    {
        std::lock_guard<std::mutex> lk(g_mu);
        g_by_cms_session[out.lSessionID] = s;
        g_sessions[key] = s;
    }
    NET_EHOME_PUSHSTREAM_IN pin{};
    pin.dwSize = sizeof pin;
    pin.lSessionID = out.lSessionID;
    NET_EHOME_PUSHSTREAM_OUT pout{};
    pout.dwSize = sizeof pout;
    if (!NET_ECMS_StartPushRealStream(user, &pin, &pout))
        LOG("[%s] StartPushRealStream xato: %u", s->path().c_str(), NET_ECMS_GetLastError());
    // Darhol I-kadr so'raymiz — dekoder kameraning navbatdagi GOP'ini kutmasin.
    NET_EHOME_MAKE_I_FRAME iframe{};
    iframe.iChannel = ch;
    iframe.dwStreamType = type;
    NET_ECMS_MakeIFrame(user, &iframe);
    s->opened = true;
    LOG("[%s] oqim so'raldi (sessiya %d)", s->path().c_str(), out.lSessionID);
    done();
}

// Hozir qaysi oqimlar ochiq turishi kerak (faqat ulangan va nomi yaroqli qurilmalar).
static std::map<std::string, std::tuple<std::string, LONG, int, int>> desired_locked() {
    std::map<std::string, std::tuple<std::string, LONG, int, int>> d;
    auto add = [&d](const std::string& dev, LONG user, int ch, int type) {
        if (!valid_id(dev) || ch < 1 || type < 0 || type > 1) return;
        d[dev + "/" + std::to_string(ch) + "/" + std::to_string(type)] = {dev, user, ch, type};
    };
    if (g_want_set) {
        for (auto& [dev, ch, type] : g_want) {
            auto it = g_devices.find(dev);
            if (it != g_devices.end()) add(dev, it->second.user, ch, type);
        }
    } else {
        for (auto& [dev, info] : g_devices)
            for (int ch : g_channels) add(dev, info.user, ch, g_stream_type);
    }
    return d;
}

// Nazoratchi: kerakli oqimlarni ochadi, o'lganini qayta ochadi (backoff bilan),
// keraksizini yopadi. PUT /want kelganda darhol uyg'onadi.
static void supervisor() {
    while (true) {
        {
            std::unique_lock<std::mutex> lk(g_mu);
            g_wake.wait_for(lk, std::chrono::seconds(3));
        }
        const double t = now_s();
        std::vector<std::shared_ptr<Session>> to_stop;
        std::vector<std::tuple<std::string, LONG, int, int>> to_start;
        std::vector<std::shared_ptr<Session>> expired;   // lock'dan tashqarida bo'shatiladi
        {
            std::lock_guard<std::mutex> lk(g_mu);
            for (auto it = g_retired.begin(); it != g_retired.end();) {
                if (t - it->first > 30) { expired.push_back(std::move(it->second)); it = g_retired.erase(it); }
                else ++it;
            }
            auto want = desired_locked();
            for (auto it = g_sessions.begin(); it != g_sessions.end();) {
                auto& s = it->second;
                const bool wanted = want.count(it->first) > 0;
                const bool got = s->last_data > 0;
                const bool dead = s->stop || (got ? t - s->last_data > 10 : t - s->t_request > 15);
                if (wanted && !dead) { ++it; continue; }
                if (wanted) {
                    // Video kelgan bo'lsa — uzilish, darhol qayta. Hech kelmagan bo'lsa
                    // (kanal yo'q, substream yo'q) — 3, 6, 12 ... 60 s kutib urinamiz.
                    int& f = g_fails[it->first];
                    f = got ? 0 : std::min(f + 1, 5);
                    g_next_try[it->first] = t + (f ? std::min(60, 3 << (f - 1)) : 0);
                    LOG("[%s] oqim to'xtadi — %s", s->path().c_str(),
                        f ? "keyinroq qayta urinamiz" : "qayta ochamiz");
                } else {
                    LOG("[%s] endi kerak emas — yopildi", s->path().c_str());
                }
                to_stop.push_back(s);
                it = g_sessions.erase(it);
            }
            for (auto& [key, w] : want) {
                if (g_sessions.count(key) || g_starting.count(key)) continue;
                auto nt = g_next_try.find(key);
                if (nt != g_next_try.end() && t < nt->second) continue;
                g_starting.insert(key);
                to_start.push_back(w);
            }
        }
        for (auto& s : to_stop) stop_session(s);
        for (auto& [dev, user, ch, type] : to_start)
            std::thread(start_session, dev, user, ch, type).detach();
    }
}

static void stats() {
    while (true) {
        std::this_thread::sleep_for(std::chrono::seconds(60));
        std::lock_guard<std::mutex> lk(g_mu);
        LOG("[holat] qurilmalar %zu, oqimlar %zu, want %s", g_devices.size(), g_sessions.size(),
            g_want_set ? std::to_string(g_want.size()).c_str() : "pilot");
        for (auto& [k, s] : g_sessions)
            LOG("[holat] %s: %.1f MB, oxirgi paket %.1fs oldin", s->path().c_str(),
                s->bytes / 1e6, s->last_data > 0 ? now_s() - s->last_data : -1.0);
    }
}

// --- Ichki HTTP API ----------------------------------------------------------
static std::string ip_channels_json(const DevInfo& d) {
    std::string o = "[";
    for (size_t i = 0; i < d.ip_channels.size(); ++i) {
        auto& c = d.ip_channels[i];
        if (i) o += ",";
        o += "{\"id\":" + std::to_string(c.id) + ",\"name\":" + json_str(c.name) +
             ",\"online\":" + (c.online ? "true" : "false") + "}";
    }
    return o + "]";
}

static std::string devices_json() {
    std::lock_guard<std::mutex> lk(g_mu);
    const double t = now_s();
    std::string o = "{\"want_set\":";
    o += g_want_set ? "true" : "false";
    o += ",\"keys_set\":";
    o += g_keys_set ? "true" : "false";
    o += ",\"default_key\":";
    o += (g_allow_default && !g_key.empty()) ? "true" : "false";
    o += ",\"rejected\":[";
    bool fr = true;
    for (auto& [dev, r] : g_rejected) {
        if (!fr) o += ",";
        fr = false;
        o += "{\"id\":" + json_str(dev) + ",\"reason\":" + json_str(r.reason) +
             ",\"count\":" + std::to_string(r.count) + ",\"age\":" + std::to_string((long)(t - r.at)) + "}";
    }
    o += "],\"devices\":[";
    bool first = true;
    for (auto& [dev, d] : g_devices) {
        if (!first) o += ",";
        first = false;
        o += "{\"id\":" + json_str(dev) + ",\"valid\":" + (valid_id(dev) ? "true" : "false") +
             ",\"serial\":" + json_str(d.serial) + ",\"full_serial\":" + json_str(d.full_serial) +
             ",\"ip\":" + json_str(d.ip) + ",\"firmware\":" + json_str(d.firmware) +
             ",\"protocol\":" + json_str(d.protocol) + ",\"key_source\":" + json_str(d.key_source) +
             ",\"info\":" + json_str(d.info) + ",\"dev_type\":" + std::to_string(d.dev_type) +
             ",\"channels_total\":" + std::to_string(d.ch_total) + ",\"channels_analog\":" + std::to_string(d.ch_analog) +
             ",\"start_channel\":" + std::to_string(d.start_ch) + ",\"ip_channels\":" + ip_channels_json(d) +
             ",\"online_seconds\":" + std::to_string((long)(t - d.since)) + ",\"streams\":[";
        bool f2 = true;
        for (auto& [k, s] : g_sessions) {
            if (s->dev != dev) continue;
            if (!f2) o += ",";
            f2 = false;
            char b[256];
            snprintf(b, sizeof b,
                     "{\"channel\":%d,\"type\":%d,\"path\":\"%s\",\"live\":%s,\"bytes\":%lld,\"age\":%.1f}",
                     s->ch, s->type, s->path().c_str(),
                     (s->last_data > 0 && t - s->last_data < 5) ? "true" : "false",
                     (long long)s->bytes, s->last_data > 0 ? t - s->last_data : -1.0);
            o += b;
        }
        o += "]}";
    }
    return o + "]}";
}

static std::string apply_want(const std::string& body) {
    std::set<WantKey> w;
    std::istringstream in(body);
    std::string line;
    int bad = 0;
    while (std::getline(in, line)) {
        std::istringstream ls(line);
        std::string dev;
        int ch = 0, type = -1;
        if (!(ls >> dev)) continue;
        if (!(ls >> ch >> type) || !valid_id(dev) || ch < 1 || ch > 512 || type < 0 || type > 1) { ++bad; continue; }
        w.emplace(dev, ch, type);
    }
    const size_t n = w.size();
    {
        std::lock_guard<std::mutex> lk(g_mu);
        const bool changed = !g_want_set || w != g_want;
        if (changed) {
            // Yangi so'ralgan oqim backoff'da qolib ketmasin (foydalanuvchi kutyapti).
            for (auto& k : w) {
                auto key = std::get<0>(k) + "/" + std::to_string(std::get<1>(k)) + "/" + std::to_string(std::get<2>(k));
                if (!g_want.count(k)) g_next_try.erase(key);
            }
            LOG("[api] want yangilandi: %zu ta oqim", w.size());
        }
        g_want.swap(w);
        g_want_set = true;
        g_want_at = now_s();
        if (changed) g_wake.notify_all();
    }
    return "{\"ok\":true,\"wanted\":" + std::to_string(n) + ",\"bad\":" + std::to_string(bad) + "}";
}

// Matn, har qatorda bittasi:
//   dev <DeviceID> <kalit>     qurilmaning kaliti (boshqaruvchi hal qiladi)
//   block <DeviceID>           rad etiladi
//   prefix <prefiks> <kalit>   noma'lum qurilma Device ID'si shu bilan boshlansa
//   default on|off             zaxira ISUP_KEY (muhitdan) ishlatilsinmi
static std::string apply_keys(const std::string& body) {
    std::map<std::string, std::string> keys;
    std::set<std::string> blocked;
    std::vector<std::pair<std::string, std::string>> prefixes;
    bool allow_default = true;
    int bad = 0;
    std::istringstream in(body);
    std::string line;
    while (std::getline(in, line)) {
        std::istringstream ls(line);
        std::string kind, a, b;
        if (!(ls >> kind)) continue;
        ls >> a >> b;
        if (kind == "dev" && valid_id(a) && valid_key(b)) keys[a] = b;
        else if (kind == "block" && !a.empty() && a.size() <= 256) blocked.insert(a);
        else if (kind == "prefix" && valid_id(a) && valid_key(b)) prefixes.emplace_back(a, b);
        else if (kind == "default" && (a == "on" || a == "off")) allow_default = a == "on";
        else ++bad;
    }
    std::sort(prefixes.begin(), prefixes.end(),
              [](auto& x, auto& y) { return x.first.size() > y.first.size(); });

    std::vector<std::pair<std::string, LONG>> kick;
    {
        std::lock_guard<std::mutex> lk(g_mu);
        const bool changed = !g_keys_set || keys != g_dev_keys || blocked != g_blocked ||
                             prefixes != g_prefixes || allow_default != g_allow_default;
        g_dev_keys.swap(keys);
        g_blocked.swap(blocked);
        g_prefixes.swap(prefixes);
        g_allow_default = allow_default;
        g_keys_set = true;
        if (changed) {
            LOG("[api] kalitlar yangilandi: qurilma %zu, prefiks %zu, bloklangan %zu, zaxira %s",
                g_dev_keys.size(), g_prefixes.size(), g_blocked.size(), g_allow_default ? "yoqiq" : "o'chiq");
            // Kaliti o'zgargan, bekor qilingan yoki bloklangan qurilma ESKI kalit bilan
            // ulanib qolmasin — chiqarib yuboramiz, qayta ulanishda yangi kalit tekshiriladi.
            for (auto& [dev, d] : g_devices) {
                auto now_key = key_for_locked(dev);
                auto used = g_auth_used.find(dev);
                if (now_key.key.empty() || (used != g_auth_used.end() && used->second.key != now_key.key))
                    kick.emplace_back(dev, d.user);
            }
        }
    }
    for (auto& [dev, user] : kick) {
        LOG("[cms] %s: kaliti o'zgardi yoki bloklandi — chiqarildi", dev.c_str());
        NET_ECMS_ForceLogout(user);
        std::lock_guard<std::mutex> lk(g_mu);
        g_devices.erase(dev);
        g_auth_used.erase(dev);
        g_wake.notify_all();
    }
    return "{\"ok\":true,\"bad\":" + std::to_string(bad) + ",\"kicked\":" + std::to_string(kick.size()) + "}";
}

static void http_reply(int c, int code, const char* status, const std::string& body) {
    std::string r = "HTTP/1.1 " + std::to_string(code) + " " + status +
                    "\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " +
                    std::to_string(body.size()) + "\r\n\r\n" + body;
    size_t off = 0;
    while (off < r.size()) {
        ssize_t n = send(c, r.data() + off, r.size() - off, MSG_NOSIGNAL);
        if (n <= 0) break;
        off += (size_t)n;
    }
}

static std::atomic<int> g_http_active{0};

// Tokenni doimiy vaqtda solishtirish (javob vaqtidan belgilab topib bo'lmasin).
static bool const_eq(const std::string& a, const std::string& b) {
    if (a.size() != b.size()) return false;
    unsigned char d = 0;
    for (size_t i = 0; i < a.size(); ++i) d |= (unsigned char)(a[i] ^ b[i]);
    return d == 0;
}

static void http_handle(int c) {
    // Butun so'rov uchun umumiy muddat (har recv uchun 5 s emas) — sekin yuboruvchi
    // ulanishni cheksiz ushlab turolmasin.
    const double deadline = now_s() + 10;
    timeval tv{5, 0};
    setsockopt(c, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    std::string req;
    char buf[8192];
    size_t hdr_end = std::string::npos;
    while (hdr_end == std::string::npos && req.size() < 65536) {
        ssize_t n = recv(c, buf, sizeof buf, 0);
        if (n <= 0 || now_s() > deadline) return;
        req.append(buf, (size_t)n);
        hdr_end = req.find("\r\n\r\n");
    }
    if (hdr_end == std::string::npos) return http_reply(c, 431, "Too Large", "{\"error\":\"header\"}");
    std::string head = req.substr(0, hdr_end), lower = head;
    std::transform(lower.begin(), lower.end(), lower.begin(), ::tolower);
    std::istringstream hs(head);
    std::string method, path;
    hs >> method >> path;

    auto header = [&](const char* name) -> std::string {
        std::string n = std::string("\r\n") + name + ":";
        auto p = lower.find(n);
        if (p == std::string::npos) return "";
        p += n.size();
        auto e = head.find("\r\n", p);
        std::string v = head.substr(p, e == std::string::npos ? std::string::npos : e - p);
        v.erase(0, v.find_first_not_of(" \t"));
        v.erase(v.find_last_not_of(" \t") + 1);
        return v;
    };
    // /health token'siz: Docker HEALTHCHECK token'ni bilmaydi va u hech narsa
    // oshkor qilmaydi. Aks holda API_TOKEN berilganda konteyner doim "unhealthy".
    if (method == "GET" && path == "/health") return http_reply(c, 200, "OK", "{\"ok\":true}");
    if (!g_api_token.empty() && !const_eq(header("x-bridge-token"), g_api_token))
        return http_reply(c, 401, "Unauthorized", "{\"error\":\"token\"}");

    size_t len = (size_t)atol(header("content-length").c_str());
    if (len > 1024 * 1024) return http_reply(c, 413, "Too Large", "{\"error\":\"body\"}");
    std::string body = req.substr(hdr_end + 4);
    while (body.size() < len) {
        ssize_t n = recv(c, buf, sizeof buf, 0);
        if (n <= 0 || now_s() > deadline) return;
        body.append(buf, (size_t)n);
    }

    if (method == "GET" && path == "/devices") return http_reply(c, 200, "OK", devices_json());
    if ((method == "PUT" || method == "POST") && path == "/want") return http_reply(c, 200, "OK", apply_want(body));
    if ((method == "PUT" || method == "POST") && path == "/keys") return http_reply(c, 200, "OK", apply_keys(body));
    http_reply(c, 404, "Not Found", "{\"error\":\"not found\"}");
}

static void http_server() {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    sockaddr_in a{};
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_ANY);
    a.sin_port = htons((uint16_t)g_api_port);
    if (bind(fd, (sockaddr*)&a, sizeof a) < 0 || listen(fd, 32) < 0) {
        LOG("API porti %d ochilmadi — backend boshqaruvi ishlamaydi (pilot rejimi qoladi)", g_api_port);
        return;
    }
    while (true) {
        int c = accept(fd, nullptr, nullptr);
        if (c < 0) continue;
        // Ichki API: ko'pi bilan 16 ta parallel so'rov — sekin ulanishlar thread to'plab
        // ko'prikni yiqitmasin (slowloris).
        if (g_http_active.load() >= 16) { close(c); continue; }
        g_http_active++;
        std::thread([c] { http_handle(c); close(c); g_http_active--; }).detach();
    }
}

// --- Qurilma ma'lumoti: NVR kanallari ---------------------------------------
// NVR bitta Device ID bilan ulanadi, lekin ichida N ta kamera (kanal) bor. Backend har
// kanal uchun kamerani o'zi qo'shishi uchun kanallar ro'yxati kerak: avval SDK'ning
// DEVICE_INFO (soni, boshlang'ich raqam), keyin ISAPI pass-through orqali NVR'ning IP
// kanallari (id, nom, onlaynmi). Callback'da EMAS — alohida oqimda (sekin bo'lishi mumkin).
static std::string isapi_get(LONG user, const std::string& url) {
    std::string req = "GET " + url;
    std::vector<char> out(512 * 1024);
    NET_EHOME_PTXML_PARAM p{};
    p.pRequestUrl = (void*)req.c_str();
    p.dwRequestUrlLen = (DWORD)req.size();
    p.pOutBuffer = out.data();
    p.dwOutSize = (DWORD)out.size();
    p.dwRecvTimeOut = 8000;
    if (!NET_ECMS_ISAPIPassThrough(user, &p)) return "";
    return std::string(out.data(), std::min<size_t>(p.dwReturnedXMLLen, out.size()));
}

// <name>…</name> ichidagi matn (birinchi uchragani, `from` dan boshlab).
static std::string xml_tag(const std::string& x, const std::string& name, size_t from = 0, size_t to = std::string::npos) {
    auto a = x.find("<" + name + ">", from);
    if (a == std::string::npos || a >= to) return "";
    a += name.size() + 2;
    auto b = x.find("</" + name + ">", a);
    if (b == std::string::npos || b > to) return "";
    return x.substr(a, b - a);
}

// Aynan `name` elementlari (atributli bo'lishi ham mumkin) — [boshi, oxiri) juftliklari.
static std::vector<std::pair<size_t, size_t>> xml_blocks(const std::string& x, const std::string& name) {
    std::vector<std::pair<size_t, size_t>> out;
    size_t p = 0;
    while ((p = x.find("<" + name, p)) != std::string::npos) {
        char c = p + 1 + name.size() < x.size() ? x[p + 1 + name.size()] : 0;
        if (c != '>' && c != ' ') { p += name.size(); continue; }   // InputProxyChannelList emas
        auto e = x.find("</" + name + ">", p);
        if (e == std::string::npos) break;
        out.emplace_back(p, e);
        p = e;
    }
    return out;
}

static void fetch_dev_info(std::string dev, LONG user) {
    std::this_thread::sleep_for(std::chrono::milliseconds(800));  // sessiya kaliti o'rnashsin
    DevInfo upd;
    bool ok = false;
    NET_EHOME_DEVICE_INFO di{};
    di.dwSize = sizeof di;
    NET_EHOME_CONFIG cfg{};
    cfg.pOutBuf = &di;
    cfg.dwOutSize = sizeof di;
    if (NET_ECMS_GetDevConfig(user, NET_EHOME_GET_DEVICE_INFO, &cfg, sizeof cfg)) {
        ok = true;
        upd.dev_type = (int)di.dwDevType;
        upd.ch_analog = (int)di.dwChannelNumber;
        upd.ch_total = (int)di.dwChannelAmount;
        upd.start_ch = di.dwStartChannel ? (int)di.dwStartChannel : 1;
    } else {
        LOG("[%s] DEVICE_INFO olinmadi: %u", dev.c_str(), NET_ECMS_GetLastError());
    }
    std::string chx = isapi_get(user, "/ISAPI/ContentMgmt/InputProxy/channels");
    if (!chx.empty()) {
        std::string stx = isapi_get(user, "/ISAPI/ContentMgmt/InputProxy/channels/status");
        std::map<int, bool> online;
        for (auto [a, b] : xml_blocks(stx, "InputProxyChannelStatus"))
            online[atoi(xml_tag(stx, "id", a, b).c_str())] = xml_tag(stx, "online", a, b) == "true";
        for (auto [a, b] : xml_blocks(chx, "InputProxyChannel")) {
            ChanInfo c;
            c.id = atoi(xml_tag(chx, "id", a, b).c_str());
            c.name = xml_tag(chx, "name", a, b);
            if (c.id <= 0) continue;
            auto it = online.find(c.id);
            c.online = it == online.end() ? true : it->second;
            upd.ip_channels.push_back(c);
        }
        ok = true;
    }
    std::lock_guard<std::mutex> lk(g_mu);
    auto it = g_devices.find(dev);
    if (it == g_devices.end() || it->second.user != user) return;  // shu orada uzilgan
    auto& d = it->second;
    d.info = ok ? "ok" : "failed";
    d.dev_type = upd.dev_type;
    d.ch_analog = upd.ch_analog;
    d.ch_total = upd.ch_total;
    d.start_ch = upd.start_ch;
    d.ip_channels = upd.ip_channels;
    LOG("[%s] qurilma: tur=%d, kanal jami=%d (analog %d, boshi %d), IP kanallar=%zu",
        dev.c_str(), d.dev_type, d.ch_total, d.ch_analog, d.start_ch, d.ip_channels.size());
}

// --- CMS (ro'yxatdan o'tish) --------------------------------------------------
static BOOL CALLBACK on_register(LONG user, DWORD type, void* out, DWORD outLen, void* in, DWORD inLen, void*) {
    switch (type) {
    case ENUM_DEV_AUTH: {  // ISUP 5.0: SDK qurilma kalitini shu yerda qaytargan kalit bilan solishtiradi
        const std::string dev = dev_from_out(out, outLen);
        AuthUse k;
        {
            std::lock_guard<std::mutex> lk(g_mu);
            if (dev.empty()) {
                // Device ID o'qilmadi — faqat zaxira kalit bilan (bo'lsa).
                k = (g_allow_default && !g_key.empty()) ? AuthUse{g_key, "default"} : AuthUse{"", "no-id"};
            } else {
                k = key_for_locked(dev);
            }
            if (k.key.empty()) note_rejected_locked(dev, k.source);
            else { k.at = now_s(); k.auth_user = user; g_auth_used[dev] = k; }
            // Eskirgan ruxsatnomalar xotirada abadiy qolmasin (ulangan qurilmalarniki
            // kalit almashganini aniqlash uchun kerak — ular qoladi).
            const double now = now_s();
            for (auto it = g_auth_used.begin(); it != g_auth_used.end();)
                it = (now - it->second.at > 600 && !g_devices.count(it->first)) ? g_auth_used.erase(it)
                                                                                 : std::next(it);
        }
        if (k.key.empty()) {
            LOG("[cms] RAD ETILDI: id=%s (%s)", log_safe(dev).c_str(),
                k.source == "blocked" ? "bloklangan" : k.source == "unknown" ? "kalit topilmadi — PUT /keys bilan kalit bering" : k.source.c_str());
            return FALSE;
        }
        if (!in || inLen == 0) return FALSE;
        memset(in, 0, inLen);
        memcpy(in, k.key.data(), std::min<size_t>(k.key.size(), inLen));
        return TRUE;
    }
    case ENUM_DEV_SESSIONKEY: {
        if (!out) return FALSE;
        auto* info = static_cast<NET_EHOME_DEV_REG_INFO_V12*>(out);
        NET_EHOME_DEV_SESSIONKEY sk{};
        memcpy(sk.sDeviceID, info->struRegInfo.byDeviceID, MAX_DEVICE_ID_LEN);
        memcpy(sk.sSessionKey, info->struRegInfo.bySessionKey, MAX_MASTER_KEY_LEN);
        NET_ECMS_SetDeviceSessionKey(&sk);
        return TRUE;
    }
    case ENUM_DEV_DAS_REQ:  // qurilma qaysi serverga ro'yxatdan o'tishini so'raydi — o'zimizni beramiz
        if (!in || inLen == 0) return FALSE;
        snprintf((char*)in, inLen,
                 "{\"Type\":\"DAS\",\"DasInfo\":{\"Address\":\"%s\",\"Domain\":\"\",\"ServerID\":\"das_%s_%d\","
                 "\"Port\":%d,\"UdpPort\":%d}}",
                 g_public_ip.c_str(), g_public_ip.c_str(), g_cms_port, g_cms_port, g_cms_port);
        return TRUE;
    case ENUM_DEV_ON: {
        if (!out) return FALSE;   // SDK ma'lumot bermagan — nullptr'ni o'qimaymiz
        auto* info = static_cast<NET_EHOME_DEV_REG_INFO_V12*>(out);
        auto& r = info->struRegInfo;
        DevInfo d;
        d.user = user;
        d.serial = bstr(r.sDeviceSerial, sizeof r.sDeviceSerial);
        d.full_serial = bstr(info->byDeviceFullSerial, sizeof info->byDeviceFullSerial);
        d.ip = bstr(r.struDevAdd.szIP, sizeof r.struDevAdd.szIP);
        d.firmware = bstr(r.byFirmwareVersion, sizeof r.byFirmwareVersion);
        d.protocol = bstr(r.byDevProtocolVersion, sizeof r.byDevProtocolVersion);
        d.since = now_s();
        std::string dev = bstr(r.byDeviceID, sizeof r.byDeviceID);
        LONG old_user = -1;
        {
            // XAVFSIZLIK: qurilma shu zahoti KALIT TEKSHIRUVIDAN (ENUM_DEV_AUTH) o'tgan
            // bo'lishi shart. Moslik rejimida (byAccessSecurity=0) SDK kalit bosqichisiz
            // eski protokol ro'yxatini ham qabul qilishi mumkin — u holda internetdagi
            // istalgan kishi prefiksli Device ID bilan "qurilma" bo'lib kirib, kameralar
            // yaratib yoki haqiqiy qurilma o'rniga o'z videosini qo'yib yuborardi.
            //
            // Ruxsatnoma BIR MARTALIK va (SDK sessiya handle'ini bersa) shu ulanishga
            // bog'langan — [[AuthUse]]. Ishlatilgan ruxsatnoma faqat o'sha sessiyaning
            // takroriy ON'iga yaraydi.
            std::lock_guard<std::mutex> lk(g_mu);
            const double now = now_s();
            auto usable = [&](const AuthUse& a) {
                if (now - a.at >= 120) return false;
                if (a.consumed) return a.on_user == user;
                return a.auth_user < 0 || a.auth_user == user;
            };
            auto au = g_auth_used.find(dev);
            bool fresh = au != g_auth_used.end() && usable(au->second);
            if (!fresh && au != g_auth_used.end() && now - au->second.at < 120)
                LOG("[cms] %s: ruxsatnoma boshqa ulanishniki (auth sessiya %ld, ishlatilgan=%d, bu sessiya %ld)",
                    log_safe(dev).c_str(), (long)au->second.auth_user, (int)au->second.consumed, (long)user);
            if (!fresh) {
                // Device ID'siz AUTH (SDK uni bermagan bo'lsa) — faqat zaxira kalitli qurilma.
                auto any = g_auth_used.find("");
                fresh = any != g_auth_used.end() && usable(any->second) && !any->second.consumed &&
                        key_for_locked(dev).source == "default";
                if (fresh) { g_auth_used[dev] = any->second; g_auth_used.erase(""); }
            }
            if (fresh && valid_id(dev)) {
                auto& a = g_auth_used[dev];
                a.consumed = true;
                a.on_user = user;
            }
            if (!valid_id(dev) || !fresh) {
                note_rejected_locked(dev, valid_id(dev) ? "no_auth" : "bad_id");
                LOG("[cms] RAD ETILDI: id=%s — %s", log_safe(dev).c_str(),
                    valid_id(dev) ? "kalit tekshiruvisiz ro'yxat (ISUP 5.0 emas?)" : "Device ID'da ruxsat etilmagan belgi");
                return FALSE;
            }
            auto ex = g_devices.find(dev);
            if (ex != g_devices.end() && ex->second.user != user) old_user = ex->second.user;
        }
        if (old_user >= 0) {
            // Bir xil Device ID bilan ikkinchi ulanish: eskisini chiqaramiz (ikkalasi bir
            // vaqtda turolmaydi — kimniki ko'rinayotgani noaniq bo'lardi). Logda ko'rinadi.
            LOG("[cms] %s: bir xil Device ID bilan qayta ulanish — eski sessiya chiqarildi", dev.c_str());
            NET_ECMS_ForceLogout(old_user);
        }
        LOG("[cms] QURILMA ULANDI: id=%s serial=%s ip=%s:%d firmware=%s protokol=%s",
            dev.c_str(), log_safe(d.serial).c_str(), log_safe(d.ip).c_str(), r.struDevAdd.wPort,
            log_safe(d.firmware).c_str(), log_safe(d.protocol).c_str());
        if (in) {
            auto* srv = static_cast<NET_EHOME_SERVER_INFO_V50*>(in);
            srv->dwKeepAliveSec = 15;
            srv->dwTimeOutCount = 4;
        }
        std::lock_guard<std::mutex> lk(g_mu);
        auto au = g_auth_used.find(dev);
        d.key_source = au != g_auth_used.end() ? au->second.source : "";
        g_rejected.erase(dev);
        g_devices[dev] = d;
        // Qayta ulangan qurilma eski backoff'ni meros olmasin.
        for (auto it = g_next_try.begin(); it != g_next_try.end();)
            it = it->first.rfind(dev + "/", 0) == 0 ? g_next_try.erase(it) : std::next(it);
        g_wake.notify_all();
        std::thread(fetch_dev_info, dev, user).detach();
        return TRUE;
    }
    case ENUM_DEV_OFF: {
        std::lock_guard<std::mutex> lk(g_mu);
        for (auto it = g_devices.begin(); it != g_devices.end(); ++it)
            if (it->second.user == user) { LOG("[cms] qurilma uzildi: %s", it->first.c_str()); g_devices.erase(it); break; }
        NET_ECMS_ForceLogout(user);
        g_wake.notify_all();
        return TRUE;
    }
    case ENUM_DEV_DAS_EHOMEKEY_ERROR:
    {
        // Qurilmadagi Encryption Key biz qaytargan kalit bilan mos emas.
        const std::string dev = dev_from_out(out, outLen);
        std::string src;
        {
            std::lock_guard<std::mutex> lk(g_mu);
            auto au = g_auth_used.find(dev);
            if (au != g_auth_used.end()) src = au->second.source;
            if (valid_id(dev)) note_rejected_locked(dev, "key_mismatch");
        }
        LOG("[cms] KALIT XATO: id=%s — qurilmadagi Encryption Key mos emas (kutilgan: %s kaliti)",
            log_safe(dev).c_str(), src.empty() ? "?" : src.c_str());
    }
        return TRUE;
    case ENUM_DEV_SESSIONKEY_ERROR:
        LOG("[cms] sessionkey almashinuvida xato");
        return TRUE;
    case ENUM_DEV_DAS_PINGREO:
    case ENUM_DEV_SESSIONKEY_REQ:
        return TRUE;
    default:
        LOG("[cms] hodisa turi=%u", type);
        return TRUE;
    }
}

static void CALLBACK on_stream_exception(DWORD type, LONG, LONG handle, void*) {
    LOG("[stream] istisno turi=0x%x handle=%d (nazoratchi qayta ochadi)", type, handle);
}

int main() {
    signal(SIGPIPE, SIG_IGN);
    g_key = env("ISUP_KEY", "");
    g_public_ip = env("PUBLIC_IP", "");
    g_cms_port = atoi(env("CMS_PORT", "17660").c_str());
    g_stream_port = atoi(env("STREAM_PORT", "17661").c_str());
    g_api_port = atoi(env("API_PORT", "8091").c_str());
    g_api_token = env("API_TOKEN", "");
    g_rtsp_base = env("RTSP_BASE", "rtsp://mediamtx:8554");
    g_mtx_api = env("MTX_API", "");
    g_stream_type = atoi(env("STREAM_TYPE", "0").c_str());
    std::stringstream ss(env("CHANNELS", "1"));
    for (std::string c; std::getline(ss, c, ',');) if (!c.empty()) g_channels.push_back(atoi(c.c_str()));
    if (!g_key.empty() && !valid_key(g_key)) { LOG("ISUP_KEY 8-32 harf/raqam bo'lishi kerak"); return 1; }
    if (g_key.empty()) LOG("ISUP_KEY yo'q — faqat backend yuborgan kalitlar (PUT /keys) ishlaydi");
    if (g_public_ip.empty()) { LOG("PUBLIC_IP berilmagan — qurilma video portiga qayerga ulanishini bilmaydi"); return 1; }

    std::string lib = env("SDK_LIB", "/opt/isup/lib");
    std::string crypto = lib + "/libcrypto.so", ssl = lib + "/libssl.so";
    NET_ECMS_SetSDKInitCfg(NET_EHOME_CMS_INIT_CFG_LIBEAY_PATH, (void*)crypto.c_str());
    NET_ECMS_SetSDKInitCfg(NET_EHOME_CMS_INIT_CFG_SSLEAY_PATH, (void*)ssl.c_str());
    NET_ESTREAM_SetSDKInitCfg(NET_EHOME_ESTREAM_INIT_CFG_LIBEAY_PATH, (void*)crypto.c_str());
    NET_ESTREAM_SetSDKInitCfg(NET_EHOME_ESTREAM_INIT_CFG_SSLEAY_PATH, (void*)ssl.c_str());

    if (!NET_ECMS_Init() || !NET_ESTREAM_Init()) { LOG("SDK init xato"); return 1; }
    // SDK logi: level 2 (xato+ogohlantirish). Pilotdagi 3 sutkasiga ~14 MB yozardi.
    int sdk_log = atoi(env("SDK_LOG_LEVEL", "2").c_str());
    char logdir[] = "/opt/isup/logs/";
    NET_ECMS_SetLogToFile(sdk_log, logdir, TRUE);
    NET_ESTREAM_SetLogToFile(sdk_log, logdir, TRUE);

    NET_EHOME_LOCAL_ACCESS_SECURITY sec{};
    sec.dwSize = sizeof sec;
    // 0 — moslik rejimi (istalgan protokol versiyasi, kalitsiz eski protokol ham),
    // 2 — xavfsiz rejim (faqat 4.0+ protokol xavfsizligi bilan). Kalitsiz ro'yxat
    // ENUM_DEV_ON'da baribir rad etiladi, shuning uchun 2 faqat hujum yuzasini
    // kamaytiradi; pilot qurilmalarda tekshirilgach ACCESS_SECURITY=2 qo'ying.
    int access = atoi(env("ACCESS_SECURITY", "0").c_str());
    if (access < 0 || access > 2) access = 0;
    sec.byAccessSecurity = (BYTE)access;
    LOG("SDK kirish rejimi: %d (%s)", access, access == 0 ? "moslik" : access == 2 ? "xavfsiz" : "oddiy");
    NET_ECMS_SetSDKLocalCfg(ACTIVE_ACCESS_SECURITY, &sec);
    NET_ESTREAM_SetSDKLocalCfg(ACTIVE_ACCESS_SECURITY, &sec);

    NET_ESTREAM_SetExceptionCallBack(0, nullptr, on_stream_exception, nullptr);

    NET_EHOME_LISTEN_PREVIEW_CFG pl{};
    snprintf(pl.struIPAdress.szIP, sizeof pl.struIPAdress.szIP, "0.0.0.0");
    pl.struIPAdress.wPort = (WORD)g_stream_port;
    pl.fnNewLinkCB = on_newlink;
    pl.byLinkMode = 0;
    if (NET_ESTREAM_StartListenPreview(&pl) < 0) {
        LOG("Stream listen xato (port %d): %u", g_stream_port, NET_ESTREAM_GetLastError());
        return 1;
    }

    NET_EHOME_CMS_LISTEN_PARAM cl{};
    snprintf(cl.struAddress.szIP, sizeof cl.struAddress.szIP, "0.0.0.0");
    cl.struAddress.wPort = (WORD)g_cms_port;
    cl.fnCB = on_register;
    cl.dwKeepAliveSec = 15;
    cl.dwTimeOutCount = 4;
    if (NET_ECMS_StartListen(&cl) < 0) {
        LOG("CMS listen xato (port %d): %u", g_cms_port, NET_ECMS_GetLastError());
        return 1;
    }
    LOG("ISUP ko'prik tayyor: ro'yxat %d (tcp/udp), video %d (tcp), API %d, tashqi IP %s",
        g_cms_port, g_stream_port, g_api_port, g_public_ip.c_str());

    std::thread(http_server).detach();
    std::thread(stats).detach();
    supervisor();
    return 0;
}
