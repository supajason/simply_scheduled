"""Core scheduling and validation tests. Run: python -m unittest discover -s tests"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'simply_scheduled' / 'app'
sys.path.insert(0, str(ROOT))
from main import Store, validate, validate_params, validate_settings, build_service_calls  # noqa: E402


class SchedulerCoreTests(unittest.TestCase):
    def base(self, entity='switch.garden_socket', action='turn_on', params=None):
        return {'name': 'Garden socket', 'entity_id': entity, 'action': action,
                'params': params or {}, 'time': '19:30', 'days': ['mon','wed'], 'enabled': True}

    def test_switch_has_only_on_and_off(self):
        result = validate(self.base())
        self.assertEqual(build_service_calls(result), [('switch','turn_on',{'entity_id':'switch.garden_socket'})])
        with self.assertRaises(ValueError):
            validate(self.base(params={'brightness_pct':50}))

    def test_no_scripts_automations_or_scenes(self):
        for entity in ('script.goodnight','automation.wake','scene.reading'):
            with self.subTest(entity=entity), self.assertRaises(ValueError):
                validate(self.base(entity))

    def test_light_brightness_and_temperature(self):
        data = validate(self.base('light.kitchen',params={'brightness_pct':60, 'color_temp_kelvin':3000}))
        self.assertEqual(build_service_calls(data)[0],('light','turn_on',{'entity_id':'light.kitchen','brightness_pct':60,'color_temp_kelvin':3000}))

    def test_light_colour_rgb_service_conversion(self):
        data=validate(self.base('light.office',params={'rgb_color':'#ff8040'}))
        self.assertEqual(build_service_calls(data)[0][2]['rgb_color'], [255,128,64])
        with self.assertRaises(ValueError):
            validate(self.base('light.office',params={'rgb_color':'#FFFFFF','color_temp_kelvin':3400}))

    def test_climate_target_temperature(self):
        data=validate(self.base('climate.office',params={'hvac_mode':'heat','temperature':20.5}))
        self.assertEqual(build_service_calls(data)[0],('climate','set_temperature',{'entity_id':'climate.office','hvac_mode':'heat','temperature':20.5}))

    def test_climate_temperature_range(self):
        data=validate(self.base('climate.house',params={'hvac_mode':'heat_cool','target_temp_low':19,'target_temp_high':23}))
        self.assertEqual(build_service_calls(data)[0][1], 'set_temperature')
        with self.assertRaises(ValueError):
            validate(self.base('climate.house',params={'target_temp_low':25,'target_temp_high':20}))

    def test_cover_position_and_close(self):
        data=validate(self.base('cover.garage',params={'position':50}))
        self.assertEqual(build_service_calls(data)[0][1], 'set_cover_position')
        data=validate(self.base('cover.garage','turn_off'))
        self.assertEqual(build_service_calls(data)[0][1], 'close_cover')

    def test_humidifier_calls(self):
        data=validate(self.base('humidifier.room',params={'humidity':50}))
        self.assertEqual([x[1] for x in build_service_calls(data)], ['turn_on', 'set_humidity'])

    def test_disallowed_options_for_off(self):
        with self.assertRaises(ValueError):
            validate(self.base('light.hall','turn_off',{'brightness_pct':70}))

    def test_settings_validation(self):
        settings=validate_settings({'group_devices':False,'group_schedules':True,'excluded_entities':['switch.wlan0_allowed_on_network_9','switch.wlan0_allowed_on_network_9']})
        self.assertEqual(settings['excluded_entities'],['switch.wlan0_allowed_on_network_9'])
        with self.assertRaises(ValueError):
            validate_settings({'group_devices':True,'group_schedules':True,'excluded_entities':['automation.foo']})

    def test_persist_and_migrate_legacy(self):
        with tempfile.TemporaryDirectory() as temp:
            dir=Path(temp)
            path=dir/'schedules.json'
            old=[validate(self.base()),{'id':'f'*32,'name':'Old script','entity_id':'script.hello','action':'turn_on',
                    'time':'20:00','days':['mon'], 'enabled':True}]
            path.write_text(json.dumps(old))
            store=Store(path,dir/'settings.json')
            store.load()
            self.assertEqual(len(store.items),1)
            self.assertEqual(json.loads(path.read_text())[0]['entity_id'],'switch.garden_socket')
            self.assertTrue((dir/'schedules-before-device-only-migration.json').exists())
            store.settings=validate_settings({'group_devices':False,'group_schedules':False,'excluded_entities':['switch.garden_socket']})
            store.save_settings()
            again=Store(path,dir/'settings.json')
            again.load()
            self.assertFalse(again.settings['group_schedules'])
            self.assertEqual(again.settings['excluded_entities'],['switch.garden_socket'])


if __name__=='__main__':
    unittest.main()
