/// Data model for NRF24 jammer per-mode configuration and info.
///
/// Matches firmware structs `NrfJamModeConfig` and `NrfJamModeInfo`
/// defined in `src/modules/nrf/NrfJammer.h`.

/// Per-mode configuration (mutable, sent/received via cmd 0x43 / notif 0xD6).
class NrfJamModeConfig {
  int paLevel;       // 0-3 (0=MIN -18dBm, 3=MAX → +20dBm with PA)
  int dataRate;      // 0=1Mbps, 1=2Mbps, 2=250Kbps
  int dwellTimeMs;   // Time on each channel in ms (0-200, 0=turbo)
  bool useFlooding;  // false=Constant Carrier (CW), true=Data Flooding
  int floodBursts;   // Number of flood packets per channel hop (1-10)

  NrfJamModeConfig({
    this.paLevel = 3,
    this.dataRate = 1,
    this.dwellTimeMs = 0,
    this.useFlooding = false,
    this.floodBursts = 3,
  });

  NrfJamModeConfig copyWith({
    int? paLevel,
    int? dataRate,
    int? dwellTimeMs,
    bool? useFlooding,
    int? floodBursts,
  }) {
    return NrfJamModeConfig(
      paLevel: paLevel ?? this.paLevel,
      dataRate: dataRate ?? this.dataRate,
      dwellTimeMs: dwellTimeMs ?? this.dwellTimeMs,
      useFlooding: useFlooding ?? this.useFlooding,
      floodBursts: floodBursts ?? this.floodBursts,
    );
  }

  /// Parse from notification map (0xD6 response).
  factory NrfJamModeConfig.fromMap(Map<String, dynamic> data) {
    return NrfJamModeConfig(
      paLevel: data['paLevel'] ?? 3,
      dataRate: data['dataRate'] ?? 1,
      dwellTimeMs: data['dwellTimeMs'] ?? 0,
      useFlooding: data['useFlooding'] ?? false,
      floodBursts: data['floodBursts'] ?? 3,
    );
  }

  /// Human-readable PA level label.
  String get paLevelLabel {
    switch (paLevel) {
      case 0: return 'MIN (-18 dBm)';
      case 1: return 'LOW (-12 dBm)';
      case 2: return 'HIGH (-6 dBm)';
      case 3: return 'MAX (0 dBm / +20 PA)';
      default: return 'Unknown';
    }
  }

  /// Human-readable data rate label.
  String get dataRateLabel {
    switch (dataRate) {
      case 0: return '1 Mbps';
      case 1: return '2 Mbps';
      case 2: return '250 Kbps';
      default: return 'Unknown';
    }
  }

  /// Human-readable strategy label.
  String get strategyLabel =>
      useFlooding ? 'Data Flooding' : 'Constant Carrier (CW)';
}

/// Static info about a jammer mode (received via cmd 0x44 / notif 0xD7).
class NrfJamModeInfo {
  final int mode;
  final String name;
  final String description;
  final int freqStartMHz;
  final int freqEndMHz;
  final int channelCount;

  const NrfJamModeInfo({
    required this.mode,
    required this.name,
    required this.description,
    required this.freqStartMHz,
    required this.freqEndMHz,
    required this.channelCount,
  });

  /// Parse from notification map (0xD7 response).
  factory NrfJamModeInfo.fromMap(Map<String, dynamic> data) {
    return NrfJamModeInfo(
      mode: data['mode'] ?? 0,
      name: data['name'] ?? '',
      description: data['description'] ?? '',
      freqStartMHz: data['freqStartMHz'] ?? 2400,
      freqEndMHz: data['freqEndMHz'] ?? 2525,
      channelCount: data['channelCount'] ?? 0,
    );
  }

  /// Frequency range as human-readable string.
  String get freqRange => '$freqStartMHz – $freqEndMHz MHz';
}

/// Local metadata for each jam mode (icons, subtitles for UI).
/// These do NOT depend on firmware — they're used to display the mode
/// list before the device has been queried via 0x44.
class NrfJamModeUiData {
  final int mode;
  final String label;
  final String shortDesc;

  const NrfJamModeUiData(this.mode, this.label, this.shortDesc);

  /// All 12 modes ordered by enum value.
  static const List<NrfJamModeUiData> allModes = [
    NrfJamModeUiData(0,  'Full Spectrum',    'All 1-124 channels'),
    NrfJamModeUiData(1,  'WiFi',             '2.4 GHz WiFi channels'),
    NrfJamModeUiData(2,  'BLE Data',         'BLE data channels'),
    NrfJamModeUiData(3,  'BLE Advertising',  'BLE advert channels 37-39'),
    NrfJamModeUiData(4,  'Bluetooth',        'Classic BT FHSS'),
    NrfJamModeUiData(5,  'USB Wireless',     'Wireless mice/keyboards'),
    NrfJamModeUiData(6,  'Video Streaming',  'FPV / video links'),
    NrfJamModeUiData(7,  'RC Controllers',   'RC remotes / drones'),
    NrfJamModeUiData(8,  'Single Channel',   'One specific channel'),
    NrfJamModeUiData(9,  'Custom Hopper',    'Custom range + step'),
    NrfJamModeUiData(10, 'Zigbee',           'Zigbee channels 11-26'),
    NrfJamModeUiData(11, 'Drone',            'Full band random hop'),
  ];
}
