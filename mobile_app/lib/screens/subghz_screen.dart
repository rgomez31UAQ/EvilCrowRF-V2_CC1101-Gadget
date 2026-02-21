import 'package:flutter/material.dart';
import '../l10n/app_localizations.dart';
import '../theme/app_colors.dart';
import 'brute_screen.dart';
import 'protopirate_screen.dart';
import 'record_screen.dart';

/// Sub-GHz wrapper screen — hosts Record, Brute, and ProtoPirate as internal tabs
class SubGhzScreen extends StatefulWidget {
  const SubGhzScreen({super.key});

  @override
  State<SubGhzScreen> createState() => _SubGhzScreenState();
}

class _SubGhzScreenState extends State<SubGhzScreen>
    with SingleTickerProviderStateMixin {
  late TabController _tabController;

  /// Per-tab accent colors: Record (red), Brute (purple), ProtoPirate (cyan)
  static const List<Color> _tabColors = [
    Color(0xFFFF1744),
    Color(0xFF9C27B0),
    Color(0xFF00BCD4),
  ];

  @override
  void initState() {
    super.initState();
    _tabController = TabController(length: 3, vsync: this);
    _tabController.addListener(() {
      if (mounted) setState(() {});
    });
  }

  @override
  void dispose() {
    _tabController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final l10n = AppLocalizations.of(context)!;
    final activeColor = _tabColors[_tabController.index];

    return Column(
      children: [
        // Tab bar header
        Container(
          decoration: const BoxDecoration(
            color: AppColors.secondaryBackground,
            border: Border(
              bottom: BorderSide(color: AppColors.borderDefault, width: 1),
            ),
          ),
          child: TabBar(
            controller: _tabController,
            indicatorColor: activeColor,
            indicatorWeight: 2,
            labelColor: AppColors.primaryText,
            unselectedLabelColor: AppColors.secondaryText,
            labelStyle: const TextStyle(
              fontWeight: FontWeight.w600,
              fontSize: 13,
            ),
            tabs: [
              _buildTab(
                icon: Icons.radio_button_checked,
                label: l10n.record,
                tabIndex: 0,
              ),
              _buildTab(
                icon: Icons.lock_open,
                label: l10n.brute,
                tabIndex: 1,
              ),
              _buildTab(
                icon: Icons.car_repair,
                label: l10n.protoPirate,
                tabIndex: 2,
              ),
            ],
          ),
        ),

        const SizedBox(height: 6),

        // Tab content
        Expanded(
          child: TabBarView(
            controller: _tabController,
            children: const [
              RecordScreen(),
              BruteScreen(),
              ProtoPirateScreen(),
            ],
          ),
        ),
      ],
    );
  }

  /// Build a tab with icon and label, colored by active state
  Widget _buildTab({
    required IconData icon,
    required String label,
    required int tabIndex,
  }) {
    final isActive = _tabController.index == tabIndex;
    final color = isActive ? _tabColors[tabIndex] : AppColors.secondaryText;
    return Tab(
      height: 40,
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 16, color: color),
          const SizedBox(width: 6),
          Text(
            label,
            style: TextStyle(
              color: color,
              fontWeight: FontWeight.w600,
              fontSize: 13,
            ),
          ),
        ],
      ),
    );
  }
}
