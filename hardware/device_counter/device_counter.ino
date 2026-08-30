/*
 * Firebreak coarse presence sensor — ESP32
 * =======================================
 *
 * Counts 802.11 probe requests in promiscuous mode and reports a COARSE
 * PRESENCE signal over serial.
 *
 * WHAT THIS IS NOT
 * ----------------
 * This is not a device count and it is not a people count. Modern phones
 * randomize their MAC address per probe burst, so the number of distinct MACs
 * seen in a window OVERCOUNTS the number of devices, often by a large and
 * unstable factor. See CALIBRATION.md. The only defensible reading of the
 * output is "more radios are probing here than were a minute ago."
 *
 * PRIVACY AND LEGAL PROPERTIES — enforced here, not just documented
 * ----------------------------------------------------------------
 *  1. NEVER TRANSMITS. The radio is configured in promiscuous (receive-only)
 *     mode. WIFI_MODE_AP is never set and no beacon, probe response, or
 *     association frame is ever emitted. Impersonating a network (an "evil
 *     twin" / rogue AP) is a federal crime; this firmware cannot do it because
 *     the code path does not exist.
 *  2. NEVER INSPECTS PAYLOADS. Only the 802.11 MAC header is read, and only
 *     the subtype field and the transmitter address. Intercepting the contents
 *     of communications is a federal crime under the Wiretap Act; this
 *     firmware reads no frame body at all.
 *  3. NO SSID CAPTURE. A probe request carries the SSID the device is looking
 *     for, which can identify a home, a workplace, or a person. This firmware
 *     steps over that field and never reads it.
 *  4. MACS ARE HASHED IMMEDIATELY. The raw address exists only as a local in
 *     the callback. It is salted and hashed before anything is stored, and the
 *     hash is truncated so it cannot be reversed by brute force against the
 *     48-bit space.
 *  5. SALT ROTATES EVERY WINDOW. A fresh random salt per window means hashes
 *     cannot be correlated across windows. A device cannot be tracked over
 *     time even with physical access to the device's memory.
 *  6. NOTHING PERSISTS. The window's hash set is wiped at every boundary.
 *     Nothing is written to flash. The only output is an aggregate integer.
 *
 * DEPLOYMENT
 * ----------
 * Only on property you control or with the property owner's permission, and
 * with signage where people would not otherwise expect it. "Technically
 * receive-only" is not the same as "appropriate to deploy anywhere."
 *
 * Board: any ESP32 (tested target: ESP32-WROOM-32).
 * Arduino core: esp32 by Espressif, v2.x or v3.x.
 */

#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_system.h>
#include <mbedtls/sha256.h>
#include <string.h>

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

static const uint32_t WINDOW_MS = 60000;    // reporting window
static const uint16_t HASH_SLOTS = 1024;    // open-addressed set capacity
static const uint16_t DWELL_MS = 180;       // per-channel dwell while hopping

// US 2.4 GHz channels. Probe requests are broadcast on whichever channel the
// device is scanning, so a single fixed channel sees only a fraction of them.
static const uint8_t CHANNELS[] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11};
static const uint8_t N_CHANNELS = sizeof(CHANNELS) / sizeof(CHANNELS[0]);

// ---------------------------------------------------------------------------
// 802.11 frame structure (header only — we never read the body)
// ---------------------------------------------------------------------------

// Frame control subtypes for management frames.
static const uint8_t SUBTYPE_PROBE_REQ = 0x04;

typedef struct {
  uint8_t  frame_ctrl_lo;
  uint8_t  frame_ctrl_hi;
  uint16_t duration;
  uint8_t  addr1[6];   // receiver  (broadcast for a probe request)
  uint8_t  addr2[6];   // transmitter — the only address we use
  uint8_t  addr3[6];   // BSSID
  uint16_t seq_ctrl;
} __attribute__((packed)) wifi_mac_header_t;

// ---------------------------------------------------------------------------
// Per-window state
// ---------------------------------------------------------------------------

static uint64_t g_slots[HASH_SLOTS];       // 0 == empty
static uint16_t g_unique_hashes = 0;       // distinct hashes this window
static uint32_t g_total_probes = 0;        // every probe frame, dupes included
static bool     g_set_saturated = false;   // capacity exceeded — count is a floor

static uint8_t  g_salt[16];                // rotated every window
static uint32_t g_window_start_ms = 0;
static uint32_t g_window_index = 0;

static uint8_t  g_channel_idx = 0;
static uint32_t g_last_hop_ms = 0;

// ---------------------------------------------------------------------------
// Hashing
// ---------------------------------------------------------------------------

static void rotate_salt() {
  esp_fill_random(g_salt, sizeof(g_salt));
}

/*
 * Salted SHA-256 of a MAC, truncated to 64 bits.
 *
 * The salt is what makes this safe. An unsalted hash of a 48-bit MAC is
 * trivially reversible by enumeration — it is not anonymisation. With a fresh
 * 128-bit random salt per window, and the salt never leaving RAM, the stored
 * value is not linkable to an address or across windows.
 */
static uint64_t hash_mac(const uint8_t *mac) {
  uint8_t digest[32];
  mbedtls_sha256_context ctx;

  mbedtls_sha256_init(&ctx);
  mbedtls_sha256_starts(&ctx, 0);            // 0 => SHA-256, not SHA-224
  mbedtls_sha256_update(&ctx, g_salt, sizeof(g_salt));
  mbedtls_sha256_update(&ctx, mac, 6);
  mbedtls_sha256_finish(&ctx, digest);
  mbedtls_sha256_free(&ctx);

  uint64_t out = 0;
  for (int i = 0; i < 8; i++) out = (out << 8) | digest[i];
  return out ? out : 1;                       // reserve 0 as the empty marker
}

/* Open-addressed insert. Returns true if this hash was new this window. */
static bool set_insert(uint64_t h) {
  uint16_t idx = (uint16_t)(h % HASH_SLOTS);
  for (uint16_t probe = 0; probe < HASH_SLOTS; probe++) {
    uint16_t i = (uint16_t)((idx + probe) % HASH_SLOTS);
    if (g_slots[i] == h) return false;        // already seen
    if (g_slots[i] == 0) {
      g_slots[i] = h;
      g_unique_hashes++;
      return true;
    }
  }
  g_set_saturated = true;                     // full: report count as a floor
  return false;
}

static void wipe_window() {
  memset(g_slots, 0, sizeof(g_slots));
  g_unique_hashes = 0;
  g_total_probes = 0;
  g_set_saturated = false;
  rotate_salt();                              // unlinkable across windows
  g_window_start_ms = millis();
  g_window_index++;
}

// ---------------------------------------------------------------------------
// Promiscuous callback
// ---------------------------------------------------------------------------

static void IRAM_ATTR sniffer_cb(void *buf, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_MGMT) return;

  const wifi_promiscuous_pkt_t *pkt = (wifi_promiscuous_pkt_t *)buf;
  const wifi_mac_header_t *hdr = (wifi_mac_header_t *)pkt->payload;

  // Frame control: bits 2-3 type, bits 4-7 subtype. Management type == 0.
  const uint8_t ftype   = (hdr->frame_ctrl_lo & 0x0C) >> 2;
  const uint8_t subtype = (hdr->frame_ctrl_lo & 0xF0) >> 4;
  if (ftype != 0 || subtype != SUBTYPE_PROBE_REQ) return;

  // Runt frame guard: must be at least a full MAC header.
  if (pkt->rx_ctrl.sig_len < (int)sizeof(wifi_mac_header_t)) return;

  g_total_probes++;

  // addr2 is the transmitter. This is the ONLY field we read beyond the
  // subtype. We deliberately do not touch the frame body, which is where the
  // SSID the device is searching for would be.
  uint8_t mac[6];
  memcpy(mac, hdr->addr2, 6);

  const uint64_t h = hash_mac(mac);
  memset(mac, 0, sizeof(mac));                // raw address gone immediately

  set_insert(h);
}

// ---------------------------------------------------------------------------
// Setup / loop
// ---------------------------------------------------------------------------

static void start_promiscuous() {
  // Station mode WITHOUT connecting. Never AP mode — see header note 1.
  WiFi.mode(WIFI_MODE_STA);
  WiFi.disconnect(true, true);                // ensure no association attempt
  esp_wifi_set_ps(WIFI_PS_NONE);

  wifi_promiscuous_filter_t filter = {};
  filter.filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT;  // management frames only
  esp_wifi_set_promiscuous_filter(&filter);

  esp_wifi_set_promiscuous_rx_cb(&sniffer_cb);
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(CHANNELS[0], WIFI_SECOND_CHAN_NONE);
}

static void hop_channel() {
  g_channel_idx = (uint8_t)((g_channel_idx + 1) % N_CHANNELS);
  esp_wifi_set_channel(CHANNELS[g_channel_idx], WIFI_SECOND_CHAN_NONE);
  g_last_hop_ms = millis();
}

static void report_window() {
  const uint32_t elapsed_s = (millis() - g_window_start_ms) / 1000;

  // CSV: window,elapsed_s,unique_hashes,total_probes,saturated
  // "unique_hashes" is NOT a device count. See CALIBRATION.md.
  Serial.printf("%lu,%lu,%u,%lu,%d\n",
                (unsigned long)g_window_index,
                (unsigned long)elapsed_s,
                (unsigned)g_unique_hashes,
                (unsigned long)g_total_probes,
                g_set_saturated ? 1 : 0);

  if (g_set_saturated) {
    Serial.println("# WARNING: hash set saturated — unique count is a FLOOR, not a count");
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);

  Serial.println("# Firebreak coarse presence sensor");
  Serial.println("# Counts WiFi probe requests. NOT a device count. NOT a people count.");
  Serial.println("# Receive-only: never transmits, never impersonates a network,");
  Serial.println("# never reads frame payloads or SSIDs. MACs are salted-hashed and");
  Serial.println("# discarded every window; the salt rotates so windows cannot be linked.");
  Serial.println("# columns: window,elapsed_s,unique_hashes,total_probes,saturated");

  memset(g_slots, 0, sizeof(g_slots));
  rotate_salt();
  g_window_start_ms = millis();
  g_last_hop_ms = millis();

  start_promiscuous();
}

void loop() {
  const uint32_t now = millis();

  if (now - g_last_hop_ms >= DWELL_MS) hop_channel();

  if (now - g_window_start_ms >= WINDOW_MS) {
    report_window();
    wipe_window();
  }

  delay(10);
}
