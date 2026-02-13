/**
 * @file NrfJammer.cpp
 * @brief 2.4 GHz jammer with per-mode tunable RF parameters.
 *
 * Hardware: E01-ML01SP2 (NRF24L01+ with PA+LNA, up to +20 dBm).
 *
 * Primary jamming strategy: **Data Flooding** (writeFast bursts).
 * Sends garbage packets at maximum throughput, creating real packet
 * collisions and CRC corruption on the target channel.  This is more
 * effective than CW in practice because modern receivers use frequency
 * diversity and error correction that resist a simple carrier tone.
 *
 * CW (Constant Carrier) is still available as a per-mode toggle for
 * special cases (e.g. disrupting analog video links).
 *
 * FHSS targets (Bluetooth, Drone) now use random channel hopping
 * instead of sequential to de-correlate the jammer's pattern from
 * the target's hopping sequence.
 *
 * Each mode has independent settings (PA, data rate, dwell time,
 * flooding toggle, flood bursts) stored in flash and configurable
 * from the app.  The **dwell time** (ms on each channel before hop)
 * is the most impactful parameter for jam effectiveness.
 *  - Too low → target escapes between hops
 *  - Too high → misses FHSS or multi-channel targets
 *
 * Channel mappings (nRF24 channel N = 2400 + N MHz):
 *  WiFi ch 1 center = 2412 → nRF ch 12, BW 22 MHz → ch 1-23
 *  BLE adv ch 37 = 2402 → nRF ch 2
 *  BLE adv ch 38 = 2426 → nRF ch 26
 *  BLE adv ch 39 = 2480 → nRF ch 80
 *  Zigbee ch 11 = 2405 → nRF ch 4-6  (each ±1 MHz)
 */

#include "NrfJammer.h"
#include "NrfModule.h"
#include "core/ble/ClientsManager.h"
#include "BinaryMessages.h"
#include "core/device_controls/DeviceControls.h"
#include "ConfigManager.h"
#include <LittleFS.h>
#include "esp_log.h"

static const char* TAG = "NrfJammer";

// Static members
volatile bool NrfJammer::running_     = false;
volatile bool NrfJammer::stopRequest_ = false;
TaskHandle_t  NrfJammer::taskHandle_  = nullptr;
NrfJamMode    NrfJammer::currentMode_ = NRF_JAM_FULL;
volatile uint8_t NrfJammer::currentChannel_ = 50;
NrfHopperConfig NrfJammer::hopperConfig_ = {0, 80, 2};
NrfJamModeConfig NrfJammer::modeConfigs_[NRF_JAM_MODE_COUNT] = {};

// Garbage payload for data flooding — 32 bytes fills the maximum nRF24 packet
// and maximises airtime (TX duty cycle) per burst.  Pattern alternates 0x55/0xAA
// to produce a pseudo-random-looking bit stream that corrupts any receiver CRC.
static const uint8_t JAM_FLOOD_DATA[32] = {
    0x55, 0xAA, 0x55, 0xAA, 0x55, 0xAA, 0x55, 0xAA,
    0xDE, 0xAD, 0xBE, 0xEF, 0xCA, 0xFE, 0xBA, 0xBE,
    0xFF, 0x00, 0xFF, 0x00, 0xA5, 0x5A, 0xA5, 0x5A,
    0x01, 0x23, 0x45, 0x67, 0x89, 0xAB, 0xCD, 0xEF
};

// Flash persistence path for per-mode configs
static const char* NRF_JAM_CONFIG_PATH = "/nrf_jam_cfg.bin";

// ── Optimal Defaults Per Mode ───────────────────────────────────
//
// These are tuned for the E01-ML01SP2 (PA+LNA) module.
// PA level 3 = chip 0dBm → module amplifies to ~+20dBm.
//
// Key insight on dwell time:
//  - WiFi frames: 1-10ms → dwell 4ms covers most frame durations
//  - BLE adv:  376µs-2ms but only 3 channels → dwell 15ms each
//  - BLE data: ~1ms packets, 40 channels → dwell 2ms balances speed/coverage
//  - BT classic: 625µs slots, 79 FHSS channels → dwell 1ms for speed
//  - Zigbee: 4ms frames, 16 channels → dwell 4ms per sub-channel
//  - Drone: random FHSS → 1ms fast random hop
//  - Full: 125 channels → 1ms = 125ms per sweep (acceptable)

void NrfJammer::setDefaults() {
    // Optimal defaults for E01-ML01SP2 (NRF24L01+ with PA+LNA, ~+20 dBm).
    //
    // Key parameters and reasoning:
    //   PA level 3   → chip 0 dBm → PA amplifies to ~+20 dBm
    //   Data rate 1   → 2 Mbps — fastest air rate, maximises packet throughput
    //                             for flooding; CW is unmodulated regardless.
    //   useFlooding 1 → Data flooding sends collision packets (per-bit corruption).
    //                   Constant Carrier (CW) creates a DC offset in demod.
    //
    //   dwellTimeMs   → Critical: how long we stay on ONE channel before hopping.
    //                   Too low → target escapes between hops.
    //                   Too high → miss other channels.
    //
    //   floodBursts   → packets written into FIFO per channel visit.
    //                   3 bursts × 32 B at 2 Mbps ≈ 384 µs airtime.
    //
    //                                    PA  DR   dwell  flood  bursts
    modeConfigs_[NRF_JAM_FULL]       = { 3,  1,   1,     1,     2 };  // Fast sweep, flood
    modeConfigs_[NRF_JAM_WIFI]       = { 3,  1,   6,     1,     5 };  // WiFi frames 1-10ms → 6ms dwel, heavy flood
    modeConfigs_[NRF_JAM_BLE]        = { 3,  1,   3,     1,     4 };  // BLE 40ch → 3ms each, strong flood
    modeConfigs_[NRF_JAM_BLE_ADV]    = { 3,  1,  20,     1,     8 };  // Only 3 ch → max dwell + burst
    modeConfigs_[NRF_JAM_BLUETOOTH]  = { 3,  1,   1,     1,     2 };  // FHSS 79ch → fast flood
    modeConfigs_[NRF_JAM_USB]        = { 3,  1,  12,     1,     5 };  // 3 ch → high dwell
    modeConfigs_[NRF_JAM_VIDEO]      = { 3,  1,  12,     1,     5 };  // 3 ch → high dwell
    modeConfigs_[NRF_JAM_RC]         = { 3,  1,  15,     1,     5 };  // 4 ch → high dwell
    modeConfigs_[NRF_JAM_SINGLE]     = { 3,  1,   1,     1,    10 };  // One channel → max bursts
    modeConfigs_[NRF_JAM_HOPPER]     = { 3,  1,   3,     1,     3 };  // Custom range → moderate
    modeConfigs_[NRF_JAM_ZIGBEE]     = { 3,  1,   5,     1,     4 };  // 48 sub-ch → 5ms
    modeConfigs_[NRF_JAM_DRONE]      = { 3,  1,   1,     1,     2 };  // Random fast hop → flood
}

// ── Channel lists for each jamming mode ─────────────────────────

// Classic Bluetooth: 21 key FHSS channels (from nRF24_jammer)
static const uint8_t JAM_BLUETOOTH_CHANNELS[] = {
    32, 34, 46, 48, 50, 52, 0, 1, 2, 4, 6,
    8, 22, 24, 26, 28, 30, 74, 76, 78, 80
};

// BLE advertising channels (the only 3 that matter for BLE discovery)
// BLE ch37=2402MHz→nRF ch2, BLE ch38=2426MHz→nRF ch26, BLE ch39=2480MHz→nRF ch80
static const uint8_t JAM_BLE_ADV_CHANNELS[] = { 2, 26, 80 };

// BLE data channels: cover the full 2402-2480 MHz range used by BLE
// data connections (channels 0-36 in BLE = nRF24 ch 2-80)
static const uint8_t JAM_BLE_CHANNELS[] = {
    2, 4, 6, 8, 10, 12, 14, 16, 18, 20,
    22, 24, 26, 28, 30, 32, 34, 36, 38, 40,
    42, 44, 46, 48, 50, 52, 54, 56, 58, 60,
    62, 64, 66, 68, 70, 72, 74, 76, 78, 80
};

// Zigbee channels 11-26: each is 2 MHz wide at 5 MHz spacing
// Zigbee ch N center = 2405 + 5*(N-11) MHz → nRF24 ch = 5 + 5*(N-11)
// We cover ±1 MHz around each center for effective jamming
static const uint8_t JAM_ZIGBEE_CHANNELS[] = {
    4, 5, 6,       // Zigbee ch 11 (2405 MHz)
    9, 10, 11,     // Zigbee ch 12 (2410 MHz)
    14, 15, 16,    // Zigbee ch 13 (2415 MHz)
    19, 20, 21,    // Zigbee ch 14 (2420 MHz)
    24, 25, 26,    // Zigbee ch 15 (2425 MHz)
    29, 30, 31,    // Zigbee ch 16 (2430 MHz)
    34, 35, 36,    // Zigbee ch 17 (2435 MHz)
    39, 40, 41,    // Zigbee ch 18 (2440 MHz)
    44, 45, 46,    // Zigbee ch 19 (2445 MHz)
    49, 50, 51,    // Zigbee ch 20 (2450 MHz)
    54, 55, 56,    // Zigbee ch 21 (2455 MHz)
    59, 60, 61,    // Zigbee ch 22 (2460 MHz)
    64, 65, 66,    // Zigbee ch 23 (2465 MHz)
    69, 70, 71,    // Zigbee ch 24 (2470 MHz)
    74, 75, 76,    // Zigbee ch 25 (2475 MHz)
    79, 80, 81     // Zigbee ch 26 (2480 MHz)
};

static const uint8_t JAM_USB_CHANNELS[] = { 40, 50, 60 };

static const uint8_t JAM_VIDEO_CHANNELS[] = { 70, 75, 80 };

static const uint8_t JAM_RC_CHANNELS[] = { 1, 3, 5, 7 };

// Full spectrum (generated at startup to save flash)
static uint8_t JAM_FULL_CHANNELS[125];
static bool fullChannelsInit = false;

static void initFullChannels() {
    if (!fullChannelsInit) {
        for (int i = 0; i < 125; i++) {
            JAM_FULL_CHANNELS[i] = i;
        }
        fullChannelsInit = true;
    }
}

// WiFi channels: each WiFi ch spans 22 MHz. We cover sub-channels across all 13.
// WiFi ch 1 center=2412→nRF ch 12, ch 6=2437→nRF ch 37, ch 11=2462→nRF ch 62
// Spread: centers + ±5 MHz offsets for better bandwidth coverage
static const uint8_t JAM_WIFI_CHANNELS[] = {
    1,  3,  5,  7,  9, 11, 13, 15, 17, 19, 21, 23,  // WiFi ch 1 (2401-2423)
   26, 28, 30, 32, 34, 36, 38, 40, 42,               // WiFi ch 6 (2426-2448)
   51, 53, 55, 57, 59, 61, 63, 65, 67, 69, 71, 73    // WiFi ch 11 (2451-2473)
};

// ── Static mode info table (compiled into flash) ────────────────

static const NrfJamModeInfo MODE_INFO_TABLE[NRF_JAM_MODE_COUNT] = {
    // NRF_JAM_FULL (0)
    { "Full Spectrum", "Sweeps all 125 channels (2400-2525 MHz). "
      "Covers everything but dwell time per channel is minimal.",
      nullptr, 125, 2400, 2525 },
    // NRF_JAM_WIFI (1)
    { "WiFi 2.4GHz", "Targets WiFi channels 1, 6, 11 (most common non-overlapping). "
      "Floods 22 MHz bandwidth per WiFi channel. 33 nRF sub-channels.",
      JAM_WIFI_CHANNELS, sizeof(JAM_WIFI_CHANNELS), 2401, 2473 },
    // NRF_JAM_BLE (2)
    { "BLE Data", "BLE data channels 0-36 mapped to nRF ch 2-80 (even). "
      "Data flooding creates packet collisions on active connections.",
      JAM_BLE_CHANNELS, sizeof(JAM_BLE_CHANNELS), 2402, 2480 },
    // NRF_JAM_BLE_ADV (3)
    { "BLE Advertising", "Only 3 BLE advertising channels: 37(2402), 38(2426), 39(2480). "
      "Blocks device discovery and pairing. High dwell = high effectiveness.",
      JAM_BLE_ADV_CHANNELS, sizeof(JAM_BLE_ADV_CHANNELS), 2402, 2480 },
    // NRF_JAM_BLUETOOTH (4)
    { "Bluetooth Classic", "Classic BT uses FHSS across 79 channels (2402-2480 MHz). "
      "CW carrier disrupts PLL lock. Fast hopping essential.",
      JAM_BLUETOOTH_CHANNELS, sizeof(JAM_BLUETOOTH_CHANNELS), 2402, 2480 },
    // NRF_JAM_USB (5)
    { "USB Wireless", "Wireless USB dongles typically use channels 40, 50, 60. "
      "CW on these 3 channels with high dwell time.",
      JAM_USB_CHANNELS, sizeof(JAM_USB_CHANNELS), 2440, 2460 },
    // NRF_JAM_VIDEO (6)
    { "Video Streaming", "Analog/digital 2.4GHz video transmitters (FPV, baby monitors). "
      "Channels 70, 75, 80 (2470-2480 MHz upper ISM band).",
      JAM_VIDEO_CHANNELS, sizeof(JAM_VIDEO_CHANNELS), 2470, 2480 },
    // NRF_JAM_RC (7)
    { "RC Controllers", "RC toys and drones on low channels 1, 3, 5, 7 (2401-2407 MHz). "
      "Few channels — high dwell time recommended.",
      JAM_RC_CHANNELS, sizeof(JAM_RC_CHANNELS), 2401, 2407 },
    // NRF_JAM_SINGLE (8)
    { "Single Channel", "Constant carrier or flood on one specific channel. "
      "Use the channel slider to target a precise frequency.",
      nullptr, 1, 2400, 2525 },
    // NRF_JAM_HOPPER (9)
    { "Custom Hopper", "User-defined channel range with configurable step size. "
      "Start/Stop/Step set from the hopper config panel.",
      nullptr, 0, 2400, 2525 },
    // NRF_JAM_ZIGBEE (10)
    { "Zigbee", "Zigbee channels 11-26 (2405-2480 MHz, 5 MHz spacing). "
      "Each Zigbee channel covered by 3 nRF sub-channels (±1 MHz). 48 total.",
      JAM_ZIGBEE_CHANNELS, sizeof(JAM_ZIGBEE_CHANNELS), 2405, 2480 },
    // NRF_JAM_DRONE (11)
    { "Drone", "Drone protocols use various FHSS schemes across 2.4 GHz. "
      "Random fast hopping with CW disrupts PLL lock on drone receivers.",
      nullptr, 125, 2400, 2525 },
};

// ── Channel list accessor ───────────────────────────────────────

const uint8_t* NrfJammer::getChannelList(NrfJamMode mode, size_t& count) {
    switch (mode) {
        case NRF_JAM_BLE:
            count = sizeof(JAM_BLE_CHANNELS);
            return JAM_BLE_CHANNELS;
        case NRF_JAM_BLE_ADV:
            count = sizeof(JAM_BLE_ADV_CHANNELS);
            return JAM_BLE_ADV_CHANNELS;
        case NRF_JAM_BLUETOOTH:
            count = sizeof(JAM_BLUETOOTH_CHANNELS);
            return JAM_BLUETOOTH_CHANNELS;
        case NRF_JAM_WIFI:
            count = sizeof(JAM_WIFI_CHANNELS);
            return JAM_WIFI_CHANNELS;
        case NRF_JAM_USB:
            count = sizeof(JAM_USB_CHANNELS);
            return JAM_USB_CHANNELS;
        case NRF_JAM_VIDEO:
            count = sizeof(JAM_VIDEO_CHANNELS);
            return JAM_VIDEO_CHANNELS;
        case NRF_JAM_RC:
            count = sizeof(JAM_RC_CHANNELS);
            return JAM_RC_CHANNELS;
        case NRF_JAM_ZIGBEE:
            count = sizeof(JAM_ZIGBEE_CHANNELS);
            return JAM_ZIGBEE_CHANNELS;
        case NRF_JAM_DRONE:
            // Drone uses random channel hopping, not a list
            count = 0;
            return nullptr;
        case NRF_JAM_FULL:
        default:
            initFullChannels();
            count = 125;
            return JAM_FULL_CHANNELS;
    }
}

// ── Per-mode config accessors ───────────────────────────────────

const NrfJamModeConfig& NrfJammer::getModeConfig(NrfJamMode mode) {
    uint8_t idx = (uint8_t)mode;
    if (idx >= NRF_JAM_MODE_COUNT) idx = 0;
    return modeConfigs_[idx];
}

bool NrfJammer::setModeConfig(NrfJamMode mode, const NrfJamModeConfig& cfg, bool persist) {
    uint8_t idx = (uint8_t)mode;
    if (idx >= NRF_JAM_MODE_COUNT) return false;

    // Clamp values to safe ranges
    NrfJamModeConfig safe = cfg;
    if (safe.paLevel > 3) safe.paLevel = 3;
    if (safe.dataRate > 2) safe.dataRate = 1;
    if (safe.dwellTimeMs < 1) safe.dwellTimeMs = 1;
    if (safe.dwellTimeMs > 200) safe.dwellTimeMs = 200;
    if (safe.useFlooding > 1) safe.useFlooding = 1;
    if (safe.floodBursts < 1) safe.floodBursts = 1;
    if (safe.floodBursts > 10) safe.floodBursts = 10;

    modeConfigs_[idx] = safe;
    ESP_LOGI(TAG, "Mode %d config: PA=%d DR=%d dwell=%dms flood=%d bursts=%d",
             idx, safe.paLevel, safe.dataRate, safe.dwellTimeMs,
             safe.useFlooding, safe.floodBursts);

    if (persist) return saveConfigs();
    return true;
}

const NrfJamModeInfo& NrfJammer::getModeInfo(NrfJamMode mode) {
    uint8_t idx = (uint8_t)mode;
    if (idx >= NRF_JAM_MODE_COUNT) idx = 0;
    return MODE_INFO_TABLE[idx];
}

// ── Flash persistence ───────────────────────────────────────────

// File format: [version:1][12 × NrfJamModeConfig structs]
// Bumped from 1→2: defaults changed to data-flooding primary strategy
#define NRF_JAM_CFG_VERSION 2

void NrfJammer::loadConfigs() {
    setDefaults();  // Always start from defaults

    if (!LittleFS.exists(NRF_JAM_CONFIG_PATH)) {
        ESP_LOGI(TAG, "No jam config file — using defaults");
        return;
    }

    File f = LittleFS.open(NRF_JAM_CONFIG_PATH, "r");
    if (!f) {
        ESP_LOGW(TAG, "Failed to open jam config");
        return;
    }

    uint8_t version = f.read();
    if (version != NRF_JAM_CFG_VERSION) {
        ESP_LOGW(TAG, "Jam config version mismatch (got %d, want %d) — using defaults",
                 version, NRF_JAM_CFG_VERSION);
        f.close();
        return;
    }

    size_t expected = sizeof(NrfJamModeConfig) * NRF_JAM_MODE_COUNT;
    size_t bytesRead = f.read((uint8_t*)modeConfigs_, expected);
    f.close();

    if (bytesRead != expected) {
        ESP_LOGW(TAG, "Jam config file truncated (%d/%d bytes) — resetting", bytesRead, expected);
        setDefaults();
        return;
    }

    // Validate and clamp each loaded config
    for (int i = 0; i < NRF_JAM_MODE_COUNT; i++) {
        auto& c = modeConfigs_[i];
        if (c.paLevel > 3) c.paLevel = 3;
        if (c.dataRate > 2) c.dataRate = 1;
        if (c.dwellTimeMs < 1) c.dwellTimeMs = 1;
        if (c.dwellTimeMs > 200) c.dwellTimeMs = 200;
        if (c.useFlooding > 1) c.useFlooding = 0;
        if (c.floodBursts < 1) c.floodBursts = 1;
        if (c.floodBursts > 10) c.floodBursts = 10;
    }

    ESP_LOGI(TAG, "Jam configs loaded from flash (%d modes)", NRF_JAM_MODE_COUNT);
}

bool NrfJammer::saveConfigs() {
    File f = LittleFS.open(NRF_JAM_CONFIG_PATH, FILE_WRITE);
    if (!f) {
        ESP_LOGE(TAG, "Failed to write jam config");
        return false;
    }

    f.write(NRF_JAM_CFG_VERSION);
    f.write((uint8_t*)modeConfigs_, sizeof(NrfJamModeConfig) * NRF_JAM_MODE_COUNT);
    f.close();

    ESP_LOGI(TAG, "Jam configs saved to flash");
    return true;
}

void NrfJammer::resetToDefaults() {
    setDefaults();
    saveConfigs();
    ESP_LOGI(TAG, "Jam configs reset to optimal defaults");
}

// ── Start/Stop ──────────────────────────────────────────────────

bool NrfJammer::start(NrfJamMode mode) {
    if (running_) {
        ESP_LOGW(TAG, "Already running");
        return false;
    }
    if (!NrfModule::isPresent()) {
        ESP_LOGE(TAG, "NRF not present");
        return false;
    }

    currentMode_ = mode;
    stopRequest_ = false;
    running_ = true;

    BaseType_t result = xTaskCreatePinnedToCore(
        jammerTask, "NrfJam", 4096, nullptr, 2, &taskHandle_, 1);

    if (result != pdPASS) {
        ESP_LOGE(TAG, "Failed to create jammer task");
        running_ = false;
        return false;
    }

    // Notify app: [JAM_STATUS][running:1][mode:1][dwellMs_lo][dwellMs_hi]
    const auto& cfg = getModeConfig(mode);
    uint8_t notif[5] = {
        MSG_NRF_JAM_STATUS, 1, (uint8_t)mode,
        (uint8_t)(cfg.dwellTimeMs & 0xFF),
        (uint8_t)((cfg.dwellTimeMs >> 8) & 0xFF)
    };
    ClientsManager::getInstance().notifyAllBinary(
        NotificationType::NrfEvent, notif, sizeof(notif));

    ESP_LOGI(TAG, "Jammer started (mode=%d dwell=%dms)", mode, cfg.dwellTimeMs);
    return true;
}

bool NrfJammer::startSingleChannel(uint8_t channel) {
    if (running_) {
        ESP_LOGW(TAG, "Already running");
        return false;
    }

    currentChannel_ = channel;
    currentMode_ = NRF_JAM_SINGLE;
    return start(NRF_JAM_SINGLE);
}

bool NrfJammer::startHopper(const NrfHopperConfig& config) {
    if (running_) {
        ESP_LOGW(TAG, "Already running");
        return false;
    }

    hopperConfig_ = config;
    currentMode_ = NRF_JAM_HOPPER;
    return start(NRF_JAM_HOPPER);
}

bool NrfJammer::setMode(NrfJamMode mode) {
    // Can change mode while running (atomic write).
    // The task loop detects the change and re-applies the mode config.
    currentMode_ = mode;
    return true;
}

bool NrfJammer::setChannel(uint8_t channel) {
    // Live channel update for SINGLE mode.
    // The task reads currentChannel_ each iteration — this takes effect
    // within 1 loop cycle (dwell time delay).
    currentChannel_ = channel;
    return true;
}

bool NrfJammer::setDwellTime(uint16_t ms) {
    if (ms < 1) ms = 1;
    if (ms > 200) ms = 200;
    // Update the current mode's config in RAM (not persisted until explicit save)
    uint8_t idx = (uint8_t)currentMode_;
    if (idx < NRF_JAM_MODE_COUNT) {
        modeConfigs_[idx].dwellTimeMs = ms;
        ESP_LOGI(TAG, "Dwell time updated to %dms (mode=%d)", ms, idx);
    }
    return true;
}

void NrfJammer::stop() {
    if (!running_) return;
    stopRequest_ = true;
    ESP_LOGI(TAG, "Jammer stop requested");
}

// ── Apply per-mode RF config to hardware ────────────────────────

void NrfJammer::applyModeConfig(NrfJamMode mode, bool flooding) {
    const auto& cfg = getModeConfig(mode);

    NrfModule::writeRegister(NRF_REG_CONFIG, NRF_PWR_UP);
    delay(2);

    NrfModule::setPALevel(cfg.paLevel);
    NrfModule::setDataRate((NrfDataRate)cfg.dataRate);
    NrfModule::writeRegister(NRF_REG_EN_AA, 0x00);      // No auto-ack for jamming
    NrfModule::writeRegister(NRF_REG_SETUP_RETR, 0x00);  // No retries
    NrfModule::disableCRC();
    NrfModule::setAddressWidth(3);     // Minimum address for speed
    NrfModule::setPayloadSize(sizeof(JAM_FLOOD_DATA));

    ESP_LOGI(TAG, "Applied config: PA=%d DR=%d dwell=%dms flood=%d bursts=%d",
             cfg.paLevel, cfg.dataRate, cfg.dwellTimeMs,
             cfg.useFlooding, cfg.floodBursts);
}

// ── Jammer Task ─────────────────────────────────────────────────
//
// The task loop reads the per-mode config each iteration so that
// changes from the app (dwell time, PA, channel) take effect live
// without requiring a stop+restart cycle.

void NrfJammer::jammerTask(void* param) {
    ESP_LOGI(TAG, "Jammer task started");

    if (!NrfModule::acquireSpi()) {
        ESP_LOGE(TAG, "SPI busy");
        running_ = false;
        vTaskDelete(nullptr);
        return;
    }

    NrfJamMode activeMode = currentMode_;
    const NrfJamModeConfig* cfg = &modeConfigs_[(uint8_t)activeMode];
    bool flooding = (cfg->useFlooding != 0);

    // Initial radio setup from current mode's config
    applyModeConfig(activeMode, flooding);

    if (flooding) {
        // TX mode: set a broadcast address, flush FIFO, ready to send
        const uint8_t txAddr[] = {0xE7, 0xE7, 0xE7};
        NrfModule::writeRegister(NRF_REG_TX_ADDR, txAddr, 3);
        NrfModule::writeRegister(NRF_REG_RX_ADDR_P0, txAddr, 3);  // ACK pipe = TX addr
        NrfModule::writeRegister(NRF_REG_CONFIG, NRF_PWR_UP);      // TX mode
        delay(2);
        NrfModule::flushTx();
        NrfModule::writeRegister(NRF_REG_STATUS, 0x70);
    } else {
        NrfModule::startConstCarrier(currentChannel_);
    }

    size_t hopIndex = 0;

    while (!stopRequest_) {
        // ── Hot-swap: detect mode change from app ───────────────
        if (activeMode != currentMode_) {
            if (!flooding) {
                NrfModule::stopConstCarrier();
            }

            activeMode = currentMode_;
            cfg = &modeConfigs_[(uint8_t)activeMode];
            flooding = (cfg->useFlooding != 0);
            hopIndex = 0;

            applyModeConfig(activeMode, flooding);

            if (flooding) {
                const uint8_t txAddr[] = {0xE7, 0xE7, 0xE7};
                NrfModule::writeRegister(NRF_REG_TX_ADDR, txAddr, 3);
                NrfModule::writeRegister(NRF_REG_RX_ADDR_P0, txAddr, 3);
                NrfModule::writeRegister(NRF_REG_CONFIG, NRF_PWR_UP);
                delay(2);
                NrfModule::flushTx();
                NrfModule::writeRegister(NRF_REG_STATUS, 0x70);
            } else {
                NrfModule::startConstCarrier(currentChannel_);
            }
        }

        // Read dwell time each iteration (allows live adjustment)
        uint16_t dwellMs = cfg->dwellTimeMs;
        uint8_t  bursts  = cfg->floodBursts;

        // ── Drone mode: random channel hopping ──────────────────
        // Random is better than sequential for FHSS targets because
        // it decorrelates from the target's hopping pattern.
        if (activeMode == NRF_JAM_DRONE) {
            uint8_t randomCh = random(125);
            if (flooding) {
                NrfModule::setChannel(randomCh);
                NrfModule::flushTx();
                NrfModule::writeRegister(NRF_REG_STATUS, 0x70);
                for (uint8_t b = 0; b < bursts; b++) {
                    NrfModule::writeFast(JAM_FLOOD_DATA, sizeof(JAM_FLOOD_DATA));
                }
            } else {
                NrfModule::ceLow();
                NrfModule::setChannel(randomCh);
                NrfModule::ceHigh();
                delayMicroseconds(150);  // PLL lock settle time
            }
            vTaskDelay(pdMS_TO_TICKS(dwellMs));
            continue;
        }

        // ── Single channel: carrier/flood on one channel ────────
        if (activeMode == NRF_JAM_SINGLE) {
            uint8_t ch = currentChannel_;
            if (flooding) {
                NrfModule::setChannel(ch);
                NrfModule::flushTx();
                NrfModule::writeRegister(NRF_REG_STATUS, 0x70);
                for (uint8_t b = 0; b < bursts; b++) {
                    NrfModule::writeFast(JAM_FLOOD_DATA, sizeof(JAM_FLOOD_DATA));
                }
            } else {
                NrfModule::ceLow();
                NrfModule::setChannel(ch);
                NrfModule::ceHigh();
            }
            vTaskDelay(pdMS_TO_TICKS(dwellMs));
            continue;
        }

        // ── Hopper mode: custom range ───────────────────────────
        if (activeMode == NRF_JAM_HOPPER) {
            uint8_t ch = currentChannel_;
            if (flooding) {
                NrfModule::setChannel(ch);
                NrfModule::flushTx();
                NrfModule::writeRegister(NRF_REG_STATUS, 0x70);
                for (uint8_t b = 0; b < bursts; b++) {
                    NrfModule::writeFast(JAM_FLOOD_DATA, sizeof(JAM_FLOOD_DATA));
                }
            } else {
                NrfModule::ceLow();
                NrfModule::setChannel(ch);
                NrfModule::ceHigh();
            }
            uint8_t nextCh = ch + hopperConfig_.stepSize;
            if (nextCh > hopperConfig_.stopChannel) {
                nextCh = hopperConfig_.startChannel;
            }
            currentChannel_ = nextCh;
            vTaskDelay(pdMS_TO_TICKS(dwellMs));
            continue;
        }

        // ── Bluetooth Classic: random hop over known BT channels ─
        // FHSS correction is better served by random ordering.
        if (activeMode == NRF_JAM_BLUETOOTH) {
            size_t count;
            const uint8_t* channels = getChannelList(NRF_JAM_BLUETOOTH, count);
            if (count > 0 && channels != nullptr) {
                uint8_t ch = channels[random(count)];
                if (flooding) {
                    NrfModule::setChannel(ch);
                    NrfModule::flushTx();
                    NrfModule::writeRegister(NRF_REG_STATUS, 0x70);
                    for (uint8_t b = 0; b < bursts; b++) {
                        NrfModule::writeFast(JAM_FLOOD_DATA, sizeof(JAM_FLOOD_DATA));
                    }
                } else {
                    NrfModule::ceLow();
                    NrfModule::setChannel(ch);
                    NrfModule::ceHigh();
                    delayMicroseconds(150);  // PLL lock
                }
            }
            vTaskDelay(pdMS_TO_TICKS(dwellMs));
            continue;
        }

        // ── Preset modes: hop through channel list sequentially ──
        size_t count;
        const uint8_t* channels = getChannelList(activeMode, count);
        if (count > 0 && channels != nullptr) {
            uint8_t ch = channels[hopIndex % count];

            if (flooding) {
                NrfModule::setChannel(ch);
                // Clear TX status/FIFO before each burst to avoid stale MAX_RT
                NrfModule::flushTx();
                NrfModule::writeRegister(NRF_REG_STATUS, 0x70);
                for (uint8_t b = 0; b < bursts; b++) {
                    NrfModule::writeFast(JAM_FLOOD_DATA, sizeof(JAM_FLOOD_DATA));
                }
            } else {
                NrfModule::ceLow();
                NrfModule::setChannel(ch);
                NrfModule::ceHigh();
                delayMicroseconds(150);  // PLL lock
            }

            hopIndex++;
            if (hopIndex >= count) hopIndex = 0;
        }

        vTaskDelay(pdMS_TO_TICKS(dwellMs));
    }

    // Cleanup
    NrfModule::stopConstCarrier();
    NrfModule::flushTx();
    NrfModule::powerDown();
    NrfModule::releaseSpi();

    running_ = false;
    taskHandle_ = nullptr;

    // Notify app that jammer stopped
    uint8_t notif[5] = { MSG_NRF_JAM_STATUS, 0, 0, 0, 0 };
    ClientsManager::getInstance().notifyAllBinary(
        NotificationType::NrfEvent, notif, sizeof(notif));

    ESP_LOGI(TAG, "Jammer task ended");
    vTaskDelete(nullptr);
}
