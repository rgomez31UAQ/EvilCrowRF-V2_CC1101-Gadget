/// Data model for a ProtoPirate decoded key fob signal.
///
/// Mirrors the firmware PPDecodeResult struct sent over BLE
/// via MSG_PP_DECODE_RESULT (0xB5) and MSG_PP_HISTORY_ENTRY (0xB6).
class ProtoPirateResult {
  /// Protocol name (e.g. "Suzuki", "Kia V3/V4", "StarLine")
  final String protocolName;

  /// Primary decoded data word
  final int data;

  /// Secondary data word (for protocols with multi-part payloads)
  final int data2;

  /// Serial / device identifier extracted from the signal
  final int serial;

  /// Button code (0-255)
  final int button;

  /// Rolling counter value (for rolling-code protocols)
  final int counter;

  /// Number of data bits decoded
  final int dataBits;

  /// Whether the signal uses encryption (KeeLoq, AUT64, AES, etc.)
  final bool encrypted;

  /// Whether CRC/checksum was valid
  final bool crcValid;

  /// Decode frequency in MHz
  final double frequency;

  /// Protocol subtype / vehicle name (optional, from result.type)
  final String? type;

  /// History index (only for history entries)
  final int? historyIndex;

  /// Timestamp in milliseconds (only for history entries)
  final int? timestampMs;

  /// Whether this result can be emulated (TX) — true for protocols with generatePulseData
  final bool canEmulate;

  const ProtoPirateResult({
    required this.protocolName,
    required this.data,
    required this.data2,
    required this.serial,
    required this.button,
    required this.counter,
    required this.dataBits,
    required this.encrypted,
    required this.crcValid,
    this.canEmulate = false,
    this.frequency = 0.0,
    this.type,
    this.historyIndex,
    this.timestampMs,
  });

  /// Human-readable data hex string
  String get dataHex {
    if (dataBits <= 32) {
      return '0x${data.toRadixString(16).toUpperCase().padLeft((dataBits / 4).ceil(), '0')}';
    }
    return '0x${data.toRadixString(16).toUpperCase()}';
  }

  /// Human-readable serial hex
  String get serialHex => '0x${serial.toRadixString(16).toUpperCase().padLeft(6, '0')}';

  /// Button name (generic mapping)
  String get buttonName {
    switch (button) {
      case 0:
        return 'None';
      case 1:
        return 'Lock';
      case 2:
        return 'Unlock';
      case 3:
        return 'Trunk';
      case 4:
        return 'Panic';
      case 5:
        return 'Start';
      default:
        return 'Btn $button';
    }
  }

  /// Compact summary line
  String get summary {
    final parts = <String>[protocolName];
    if (type != null && type!.isNotEmpty) parts.add(type!);
    parts.add(dataHex);
    if (encrypted) parts.add('ENC');
    if (crcValid) parts.add('CRC✓');
    return parts.join(' · ');
  }

  /// Protocols that support TX (emulate) — names must match firmware registry
  static const Set<String> _txCapableProtocols = {
    'Kia V0',
    'Kia V1',
    'Kia V2',
    'Ford V0',
    'Fiat V0',
    'Suzuki',
    'Subaru',
    'StarLine',
  };

  /// Create from parsed BLE binary data map
  factory ProtoPirateResult.fromMap(Map<String, dynamic> map) {
    final name = map['protocolName'] as String? ?? 'Unknown';
    // Auto-detect canEmulate from protocol name if not explicitly provided
    final emulate =
        map['canEmulate'] as bool? ?? _txCapableProtocols.contains(name);
    return ProtoPirateResult(
      protocolName: name,
      data: map['data'] as int? ?? 0,
      data2: map['data2'] as int? ?? 0,
      serial: map['serial'] as int? ?? 0,
      button: map['button'] as int? ?? 0,
      counter: map['counter'] as int? ?? 0,
      dataBits: map['dataBits'] as int? ?? 0,
      encrypted: map['encrypted'] as bool? ?? false,
      crcValid: map['crcValid'] as bool? ?? false,
      canEmulate: emulate,
      frequency: (map['frequency'] as num?)?.toDouble() ?? 0.0,
      type: map['type'] as String?,
      historyIndex: map['historyIndex'] as int?,
      timestampMs: map['timestampMs'] as int?,
    );
  }

  Map<String, dynamic> toMap() {
    return {
      'protocolName': protocolName,
      'data': data,
      'data2': data2,
      'serial': serial,
      'button': button,
      'counter': counter,
      'dataBits': dataBits,
      'encrypted': encrypted,
      'crcValid': crcValid,
      'canEmulate': canEmulate,
      'frequency': frequency,
      if (type != null) 'type': type,
      if (historyIndex != null) 'historyIndex': historyIndex,
      if (timestampMs != null) 'timestampMs': timestampMs,
    };
  }
}
