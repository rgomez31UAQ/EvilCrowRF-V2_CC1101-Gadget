/**
 * @file NrfSpectrum.cpp
 * @brief 2.4 GHz spectrum analyzer implementation.
 *
 * Uses the nRF24L01+ RPD (Received Power Detector) register to detect
 * signal presence on each channel. Applies exponential moving average
 * for smooth visualization (like the BRUCE firmware approach).
 */

#include "NrfSpectrum.h"
#include "NrfModule.h"
#include "core/ble/ClientsManager.h"
#include "BinaryMessages.h"
#include "esp_log.h"

static const char* TAG = "NrfSpectrum";

// Static members
volatile bool NrfSpectrum::running_     = false;
volatile bool NrfSpectrum::stopRequest_ = false;
TaskHandle_t  NrfSpectrum::taskHandle_  = nullptr;
uint8_t       NrfSpectrum::channelLevels_[NRF_SPECTRUM_CHANNELS] = {};

bool NrfSpectrum::start() {
    if (running_) {
        ESP_LOGW(TAG, "Already running");
        return false;
    }
    if (!NrfModule::isPresent()) {
        ESP_LOGE(TAG, "NRF not present");
        return false;
    }

    stopRequest_ = false;
    running_ = true;
    memset(channelLevels_, 0, sizeof(channelLevels_));

    BaseType_t result = xTaskCreatePinnedToCore(
        spectrumTask, "NrfSpec", 3072, nullptr, 2, &taskHandle_, 1);

    if (result != pdPASS) {
        ESP_LOGE(TAG, "Failed to create spectrum task");
        running_ = false;
        return false;
    }

    ESP_LOGI(TAG, "Spectrum analyzer started");
    return true;
}

void NrfSpectrum::stop() {
    if (!running_) return;
    stopRequest_ = true;
    ESP_LOGI(TAG, "Spectrum stop requested");
}

void NrfSpectrum::getLevels(uint8_t* levels) {
    memcpy(levels, channelLevels_, NRF_SPECTRUM_CHANNELS);
}

void NrfSpectrum::scanOnce() {
    // Must be called with SPI mutex held.
    //
    // The E01-ML01SP2 module has a PA+LNA that amplifies incoming signals
    // significantly.  The nRF24L01+ RPD register triggers at -64 dBm
    // referred to the chip input, but the LNA lowers the effective
    // threshold at the antenna to roughly -90 dBm, which means ambient
    // 2.4 GHz noise (WiFi, BLE, microwaves) trips RPD on almost every
    // channel in a single sample.
    //
    // To produce a useful spectrum display we take multiple RPD samples
    // per channel and feed the hit-ratio into a slow-rise / fast-decay
    // EMA so that only channels with persistent strong activity show
    // high bars.

    NrfModule::ceLow();

    static const int SAMPLES_PER_CH = 3;  // RPD samples per channel

    for (int i = 0; i < NRF_SPECTRUM_CHANNELS; i++) {
        NrfModule::setChannel(i);

        int hits = 0;
        for (int s = 0; s < SAMPLES_PER_CH; s++) {
            // Restart RX to clear previous RPD latch
            NrfModule::writeRegister(NRF_REG_CONFIG,
                NRF_PWR_UP | NRF_PRIM_RX);
            NrfModule::ceHigh();
            delayMicroseconds(170);  // 130 µs RX settle + 40 µs sample
            NrfModule::ceLow();

            if (NrfModule::testRPD()) hits++;
        }

        // Convert hit count to a 0-100 "strength" value.
        // Only channels that trigger RPD on ALL samples get full score.
        int rpd_pct = (hits * 100) / SAMPLES_PER_CH;

        // Asymmetric EMA: slow rise (7/8) keeps noise floor low,
        // moderate decay lets real signals stand out.
        //  Rise to 90 %: ~18 consecutive full-score sweeps  (~1.4 s)
        //  Decay from 100→0: ~30 sweeps  (~2.3 s)
        channelLevels_[i] = (uint8_t)((channelLevels_[i] * 7 + rpd_pct) / 8);
    }
}

void NrfSpectrum::spectrumTask(void* param) {
    ESP_LOGI(TAG, "Spectrum task started");

    // Notification buffer: [msgType:1][80 channel levels]
    uint8_t notifBuf[1 + NRF_SPECTRUM_CHANNELS];
    notifBuf[0] = MSG_NRF_SPECTRUM_DATA;

    uint32_t notifyCounter = 0;

    while (!stopRequest_) {
        if (!NrfModule::acquireSpi(pdMS_TO_TICKS(100))) {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        // Configure radio for wideband spectrum sensing
        NrfModule::writeRegister(NRF_REG_EN_AA, 0x00);    // No auto-ack
        NrfModule::writeRegister(NRF_REG_SETUP_AW, 0x01); // 3-byte address (minimum valid)
        NrfModule::setDataRate(NRF_1MBPS);

        // Open 6 reading pipes at noise-detection addresses exactly like
        // the BRUCE reference firmware. More pipes = higher sensitivity
        // because the radio checks all pipe addresses in parallel.
        const uint8_t noiseAddr0[] = {0x55, 0x55, 0x55};
        const uint8_t noiseAddr1[] = {0xAA, 0xAA, 0xAA};
        NrfModule::writeRegister(NRF_REG_RX_ADDR_P0, noiseAddr0, 3);
        NrfModule::writeRegister(NRF_REG_RX_ADDR_P1, noiseAddr1, 3);
        // Pipes 2-5 share address bytes [1..N] with pipe 1, only byte 0 differs
        NrfModule::writeRegister(0x0C, 0xA0); // RX_ADDR_P2
        NrfModule::writeRegister(0x0D, 0xAB); // RX_ADDR_P3
        NrfModule::writeRegister(0x0E, 0xAC); // RX_ADDR_P4
        NrfModule::writeRegister(0x0F, 0xAD); // RX_ADDR_P5
        NrfModule::writeRegister(NRF_REG_EN_RXADDR, 0x3F); // Enable all 6 pipes

        // Perform one full sweep
        scanOnce();

        NrfModule::powerDown();
        NrfModule::releaseSpi();

        // Send levels via BLE notification every 2nd sweep for smooth
        // real-time display (was every 3rd — too laggy for spectrum viz)
        notifyCounter++;
        if (notifyCounter % 2 == 0) {
            memcpy(notifBuf + 1, channelLevels_, NRF_SPECTRUM_CHANNELS);
            ClientsManager::getInstance().notifyAllBinary(
                NotificationType::NrfEvent, notifBuf, sizeof(notifBuf));
        }

        // ~25ms between scans gives ~40fps effective update rate
        vTaskDelay(pdMS_TO_TICKS(25));
    }

    // Cleanup
    running_ = false;
    taskHandle_ = nullptr;
    ESP_LOGI(TAG, "Spectrum task ended");
    vTaskDelete(nullptr);
}
