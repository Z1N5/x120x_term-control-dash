import unittest

import x120x_dashboard as dashboard


def sample(**changes):
    report, _ = dashboard.demo_data()
    report.update(current_capacity=80, current_voltage=4.0,
                  min_capacity=20, min_voltage=3.5)
    report.update(changes)
    return report


class RuntimeTests(unittest.TestCase):
    def test_two_3000mah_cells_at_80_percent_to_20_percent(self):
        model = dashboard.RuntimeModel()
        self.assertAlmostEqual(model.estimate_hours(sample()), 22.2 * .6 * .85 / 5)
        self.assertEqual(dashboard.runtime_text(model.estimate_hours(sample())), '~2h 15m')

    def test_live_charge_changes_estimate_without_using_charge_target(self):
        model = dashboard.RuntimeModel()
        self.assertAlmostEqual(model.estimate_hours(sample(current_capacity=96, max_capacity=80)),
                               22.2 * .76 * .85 / 5)

    def test_assumed_load_and_pack_size_are_configurable(self):
        normal = dashboard.RuntimeModel().estimate_hours(sample())
        self.assertAlmostEqual(dashboard.RuntimeModel(load_watts=10).estimate_hours(sample()), normal/2)
        self.assertAlmostEqual(dashboard.RuntimeModel(battery_wh=44.4).estimate_hours(sample()), normal*2)

    def test_zero_at_capacity_or_voltage_cutoff(self):
        for changes in (dict(current_capacity=20), dict(current_capacity=15),
                        dict(current_voltage=3.5), dict(current_voltage=3.4)):
            with self.subTest(changes=changes):
                self.assertEqual(dashboard.RuntimeModel().estimate_hours(sample(**changes)), 0)

    def test_disabled_voltage_cutoff_still_uses_capacity_reserve(self):
        self.assertGreater(dashboard.RuntimeModel().estimate_hours(sample(min_voltage=0, current_voltage=3.4)), 0)

    def test_stale_stopped_and_error_data_have_no_runtime_estimate(self):
        for changes in (dict(stale=True), dict(health='stopped'), dict(health='error')):
            with self.subTest(changes=changes):
                self.assertIsNone(dashboard.RuntimeModel().estimate_hours(sample(**changes)))

    def test_invalid_or_missing_inputs_have_no_estimate(self):
        for changes in (dict(current_capacity=None), dict(current_capacity=float('nan')),
                        dict(min_capacity=None), dict(current_voltage=None),
                        dict(min_voltage=None), dict(current_capacity=-1), dict(current_capacity=111)):
            with self.subTest(changes=changes):
                self.assertIsNone(dashboard.RuntimeModel().estimate_hours(sample(**changes)))

    def test_fuel_gauge_over_100_does_not_create_extra_energy(self):
        model = dashboard.RuntimeModel()
        self.assertEqual(model.estimate_hours(sample(current_capacity=110)),
                         model.estimate_hours(sample(current_capacity=100)))

    def test_invalid_model_parameters_are_rejected(self):
        for changes in (dict(battery_wh=0), dict(load_watts=-1), dict(efficiency=0),
                        dict(efficiency=1.1), dict(load_watts=float('nan')),
                        dict(battery_wh=float('inf'))):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                dashboard.RuntimeModel(**changes).validate()

    def test_large_inputs_cannot_display_infinite_runtime(self):
        self.assertIsNone(dashboard.RuntimeModel(battery_wh=1e308, load_watts=1e-308).estimate_hours(sample()))

    def test_estimate_is_independent_of_shutdown_timer_and_pmic(self):
        report = sample(charger_present=False, automatic_shutdown=True,
                        battery_runtime_remaining_seconds=244)
        _, metrics = dashboard.demo_data()
        before = dashboard.RuntimeModel().estimate_hours(report)
        text = '\n'.join(line for line, _ in dashboard.rows(report, metrics))
        self.assertIn('Est. battery runtime: ~2h 15m', text)
        self.assertIn('Shutdown timer limit: 5.00 minutes', text)
        self.assertIn('Shutdown timer remaining: 244 seconds', text)
        self.assertIn('assumed 5W total', text)
        report['battery_runtime_remaining_seconds'] = 1
        metrics['pmic_watts'] = 100
        self.assertEqual(dashboard.RuntimeModel().estimate_hours(report), before)

    def test_mains_estimate_is_labelled_backup_and_no_disabled_shutdown_countdown(self):
        report = sample(automatic_shutdown=False, battery_runtime_remaining_seconds=100)
        _, metrics = dashboard.demo_data()
        text = '\n'.join(line for line, _ in dashboard.rows(report, metrics))
        self.assertIn('Est. battery backup:', text)
        self.assertNotIn('Shutdown timer remaining:', text)

    def test_live_shutdown_countdown_accounts_for_sample_age(self):
        report = sample(charger_present=False, automatic_shutdown=True,
                        battery_runtime_remaining_seconds=244, age_seconds=2)
        _, metrics = dashboard.demo_data()
        text = '\n'.join(line for line, _ in dashboard.rows(report, metrics))
        self.assertIn('Shutdown timer remaining: 242 seconds', text)

    def test_short_durations_do_not_imply_precise_measurement(self):
        self.assertEqual(dashboard.runtime_text(.05), 'under 5 min (rough estimate)')
        self.assertIn('threshold reached', dashboard.runtime_text(0))
        self.assertEqual(dashboard.runtime_text(None), 'Unavailable')


if __name__ == '__main__':
    unittest.main()
