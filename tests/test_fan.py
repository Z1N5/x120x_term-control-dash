import contextlib
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock,patch

import x120x_fan as fan
import x120x_dashboard as dashboard


class FanCurveTests(unittest.TestCase):
    def test_starter_curve_is_valid_and_has_full_speed_final_stage(self):
        curve=fan.FanCurve()
        curve.validate()
        self.assertEqual(curve.pwm()[-1],255)
        self.assertEqual(curve.parameters()['fan_temp2'],'67500')

    def test_invalid_curves_are_rejected(self):
        cases=[dict(temperatures=(50,40,60,75)),dict(temperatures=(20,50,60,75)),
               dict(temperatures=(40,50,60,80)),dict(speeds=(50,30,70,100)),
               dict(speeds=(30,50,70,80)),dict(speeds=(0,50,70,100)),
               dict(hysteresis=(0,5,5,5)),dict(hysteresis=(5,5,5,20)),
               dict(temperatures=(50,60,float('nan'),75)),dict(speeds=(30,50))]
        for values in cases:
            with self.subTest(values=values),self.assertRaises(ValueError):
                fan.FanCurve(**values).validate()

    def test_scalar_hysteresis_expands_and_raw_pwm_not_rpm(self):
        self.assertEqual(fan.parse_values('5',repeat=True),(5,5,5,5))
        self.assertEqual(fan.parse_values('50,60,67.5,75'),(50,60,67.5,75))
        self.assertIn('PWM',fan.FanCurve().summary()[0])

    def test_boot_settings_are_scoped_and_preserve_original_content(self):
        original='# storage\n[pi5]\ndtparam=pciex1\n[none]\narm_freq=1\n'
        updated=fan.updated_boot_config(original,fan.FanCurve())
        self.assertTrue(updated.startswith(original))
        self.assertIn(fan.BEGIN+'\n[all]\n[pi5]\n',updated)
        self.assertTrue(updated.endswith('[all]\n'+fan.END+'\n'))
        self.assertEqual(fan.saved_parameters(updated),fan.FanCurve().parameters())
        self.assertTrue(all(len(line)<=98 for line in updated.splitlines() if line.startswith('dtparam=')))
        # A real parameter must precede closing overlay scope, so HAT auto-loading
        # is not inadvertently suppressed when config.txt had no earlier dtparams.
        body=updated[updated.index(fan.BEGIN):]
        self.assertLess(body.index('dtparam=cooling_fan=on'),body.index('dtoverlay='))
        self.assertIn('dtoverlay=\ndtparam=cooling_fan=on',body)

    def test_repeated_update_replaces_one_block(self):
        first=fan.updated_boot_config('dtparam=i2c_arm=on\n',fan.FanCurve())
        new=fan.FanCurve(temperatures=(40,50,60,70))
        second=fan.updated_boot_config(first,new)
        self.assertEqual(second.count(fan.BEGIN),1)
        self.assertEqual(fan.saved_parameters(second),new.parameters())
        self.assertIn('dtparam=i2c_arm=on\n',second)

    def test_malformed_block_or_separate_fan_overlay_is_not_rewritten(self):
        for content in (fan.BEGIN+'\n',fan.END+'\n',fan.BEGIN+'\n'+fan.BEGIN+'\n'+fan.END+'\n',
                        'dtoverlay=pwm-fan,fan_gpio_18\n'):
            with self.subTest(content=content),self.assertRaises(ValueError):
                fan.updated_boot_config(content,fan.FanCurve())


class BootWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.config=self.root/'config.txt'
        self.original='dtparam=i2c_arm=on\n[all]\narm_64bit=1\n'
        self.config.write_text(self.original)
        self.model=self.root/'model'
        self.model.write_text('Raspberry Pi 5 Model B Rev 1.0\0')

    def apply(self,answers):
        with contextlib.redirect_stdout(io.StringIO()):
            return fan.apply_curve(fan.FanCurve(),Mock(side_effect=answers),self.config,self.model)

    def test_either_confirmation_cancel_never_writes_or_backs_up(self):
        for answers in (['no'],['yes','wrong phrase']):
            with self.subTest(answers=answers):
                self.assertFalse(self.apply(answers))
                self.assertEqual(self.config.read_text(),self.original)
                self.assertEqual(len(list(self.root.iterdir())),2)

    def test_confirmed_curve_saves_backup_without_reboot_or_service_command(self):
        with patch('subprocess.run') as run:
            self.assertTrue(self.apply(['yes','APPLY FAN CURVE']))
        run.assert_not_called()
        backups=list(self.root.glob('config.txt.before-fan-*'))
        self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_text(),self.original)
        self.assertEqual(fan.saved_parameters(self.config.read_text()),fan.FanCurve().parameters())
        self.assertEqual(len(list(self.root.glob('.x120x-fan-*'))),0)

    def test_wrong_board_refuses_before_prompt(self):
        self.model.write_text('Raspberry Pi 4 Model B\0')
        ask=Mock()
        with self.assertRaises(ValueError):
            fan.apply_curve(fan.FanCurve(),ask,self.config,self.model)
        ask.assert_not_called()
        self.assertEqual(self.config.read_text(),self.original)

    def test_concurrent_boot_edit_is_preserved(self):
        def ask(prompt):
            if '1 of 2' in prompt:
                return 'yes'
            self.config.write_text(self.original+'# changed externally\n')
            return 'APPLY FAN CURVE'
        with contextlib.redirect_stdout(io.StringIO()),self.assertRaisesRegex(RuntimeError,'changed during'):
            fan.apply_curve(fan.FanCurve(),ask,self.config,self.model)
        self.assertTrue(self.config.read_text().endswith('# changed externally\n'))
        self.assertEqual(len(list(self.root.glob('*.before-fan-*'))),0)

    def test_restore_os_control_requires_both_confirmations_and_preserves_original(self):
        self.apply(['yes','APPLY FAN CURVE'])
        before=self.config.read_text()
        for answers in (['no'],['yes','wrong']):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(fan.restore_os_control(Mock(side_effect=answers),self.config))
            self.assertEqual(self.config.read_text(),before)
        with contextlib.redirect_stdout(io.StringIO()),patch('subprocess.run') as run:
            self.assertTrue(fan.restore_os_control(Mock(side_effect=['yes','RESTORE OS FAN CONTROL']),self.config))
        run.assert_not_called()
        self.assertEqual(self.config.read_text(),self.original)

    def test_resaving_same_curve_never_writes_or_prompts(self):
        self.apply(['yes','APPLY FAN CURVE'])
        ask=Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(fan.apply_curve(fan.FanCurve(),ask,self.config,self.model))
        ask.assert_not_called()

    def test_saved_and_active_status_are_not_confused(self):
        self.apply(['yes','APPLY FAN CURVE'])
        with patch.object(fan,'active_parameters',return_value=None):
            self.assertIn('active curve unavailable',fan.curve_status(self.config))
        values=fan.FanCurve().parameters()
        with patch.object(fan,'active_parameters',return_value=values):
            self.assertEqual(fan.curve_status(self.config),'Balanced: saved curve is active')
        values['fan_temp0']='45000'
        with patch.object(fan,'active_parameters',return_value=values):
            self.assertIn('not active',fan.curve_status(self.config))

    def test_reading_active_curve_uses_kernel_values(self):
        zone=self.root/'class/thermal/thermal_zone0'
        zone.mkdir(parents=True)
        levels=self.root/'firmware/devicetree/base/cooling_fan/cooling-levels'
        levels.parent.mkdir(parents=True)
        levels.write_bytes(struct.pack('>5I',0,75,125,175,250))
        for i,t in enumerate((50000,60000,67500,75000)):
            (zone/f'trip_point_{i}_type').write_text('active\n')
            (zone/f'trip_point_{i}_temp').write_text(str(t))
            (zone/f'trip_point_{i}_hyst').write_text('5000')
        values=fan.active_parameters(self.root)
        self.assertEqual(values['fan_temp2'],'67500')
        self.assertEqual(values['fan_temp3_speed'],'250')


class FanMenuTests(unittest.TestCase):
    def screen(self,keys):
        screen=Mock()
        screen.getmaxyx.return_value=(12,80)
        screen.getch.side_effect=keys
        return screen

    def test_fan_choice_does_not_change_shutdown_menu(self):
        report,_=dashboard.demo_data()
        self.assertEqual(dashboard.settings_menu(self.screen([ord('2')]),report),'fan')

    def test_custom_curve_wizard_accepts_defaults_without_applying(self):
        with patch.object(fan,'proposed_curve',return_value=(fan.FanCurve(),'Starter curve')):
            result=dashboard.fan_settings_menu(self.screen([ord('5')]+[10]*9))
        self.assertEqual(result,fan.FanCurve())

    def test_os_control_mode_is_explicit(self):
        self.assertEqual(dashboard.fan_settings_menu(self.screen([ord('1')])),'os')
        self.assertEqual(dashboard.fan_settings_command('os',is_root=False),
            ['sudo','--',dashboard.SETTINGS_HELPER,'restore-os-fan'])

    def test_escape_cancels_fan_wizard(self):
        self.assertIsNone(dashboard.fan_settings_menu(self.screen([27])))
        self.assertIsNone(dashboard.fan_settings_menu(self.screen([ord('5'),27])))

    def test_presets_select_valid_curves_without_writing(self):
        for key,name in ((ord('2'),'balanced'),(ord('3'),'cooler'),(ord('4'),'quiet'),(10,'balanced')):
            with self.subTest(name=name),patch.object(fan,'apply_curve') as apply:
                curve=dashboard.fan_settings_menu(self.screen([key]))
                self.assertEqual(curve,fan.FAN_PRESETS[name])
                curve.validate()
                self.assertEqual(curve.speeds[-1],100)
                self.assertLessEqual(curve.temperatures[-1],75)
                self.assertEqual(curve.hysteresis,(5,5,5,5))
                apply.assert_not_called()

    def test_saved_preset_is_identified_without_extra_configuration(self):
        for name,curve in fan.FAN_PRESETS.items():
            self.assertEqual(fan.curve_name(curve.parameters()),name.capitalize())
        custom=fan.FanCurve((40,50,60,70))
        self.assertEqual(fan.curve_name(custom.parameters()),'Custom')

    def test_cooler_and_quiet_are_distinct_from_balanced(self):
        balanced=fan.FAN_PRESETS['balanced']
        cooler=fan.FAN_PRESETS['cooler']
        quiet=fan.FAN_PRESETS['quiet']
        self.assertLess(cooler.temperatures[0],balanced.temperatures[0])
        self.assertGreater(quiet.temperatures[0],balanced.temperatures[0])
        self.assertGreater(cooler.speeds[0],balanced.speeds[0])

    def test_custom_curve_uses_fixed_helper_and_validated_arguments(self):
        command=dashboard.fan_settings_command(fan.FanCurve(),is_root=False)
        self.assertEqual(command[:4],['sudo','--',dashboard.SETTINGS_HELPER,'set-fan-curve'])
        self.assertIn('50,60,67.5,75',command)
        self.assertIn('30,50,70,100',command)


if __name__=='__main__':
    unittest.main()
