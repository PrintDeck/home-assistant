"""Print event validation, source routing and transport recovery regressions."""
import copy
import importlib
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch
from test_api import API
from test_mqtt import mqtt_info, printer_payload, ROOT
import test_mqtt_lifecycle as lifecycle

EVENTS=importlib.import_module('printdeck.print_events')
HUB=importlib.import_module('printdeck.event_hub')
STATE=importlib.import_module('printdeck.mqtt_state')
STREAM='0123456789abcdef'

def journal(sequence=0, now=1000, stream=STREAM):
    records=[] if not sequence else [dict(event_type='milestone',stream_id=stream,
        sequence=sequence,job_id=stream+'-1',observed_at_ms=now,job_kind='print',
        progress_percent=27,milestone=25,condition='normal')]
    return dict(stream_id=stream,sequence=sequence,job_id=stream+'-1',observed_at_ms=now,events=records)

class CursorTests(unittest.TestCase):
    def test_startup_duplicates_out_of_order_and_new_streams(self):
        cursor=EVENTS.PrintEventCursor()
        self.assertEqual(cursor.consume('1',EVENTS.parse_print_events(journal(3))),())
        data=EVENTS.parse_print_events(journal(4))
        self.assertEqual(len(cursor.consume('1',data)),1)
        self.assertEqual(cursor.consume('1',data),())
        self.assertEqual(cursor.consume('1',EVENTS.parse_print_events(journal(2))),())
        self.assertEqual(cursor.consume('1',data),())
        self.assertEqual(cursor.consume('1',EVENTS.parse_print_events(journal(8,stream='fedcba9876543210'))),())
        cursor.reset()
        self.assertEqual(cursor.consume('1',data),())

    def test_stale_expired_and_removed_profiles(self):
        cursor=EVENTS.PrintEventCursor();cursor.consume('1',EVENTS.parse_print_events(journal()))
        data=journal(1);data['observed_at_ms']=32000
        self.assertEqual(cursor.consume('1',EVENTS.parse_print_events(data)),())
        self.assertEqual(cursor.consume('1',EVENTS.parse_print_events(journal(2)),True),())
        cursor.prune(set())
        self.assertEqual(cursor.consume('1',EVENTS.parse_print_events(journal(3))),())

    def test_malformed_records_and_bounds(self):
        bad=[]
        for key,value in [('sequence',True),('stream_id','wrong'),('job_id','other-1'),('events',[{}]*9),('observed_at_ms',-1)]:
            item=journal(1);item[key]=value;bad.append(item)
        for key,value in [('sequence',0),('sequence',2),('stream_id','0'*16),('event_type','mystery'),('milestone',50.5),('progress_percent',float('nan')),('observed_at_ms',1001),('job_id',STREAM+'-4294967296')]:
            item=journal(1);item['events'][0][key]=value;bad.append(item)
        for item in bad:
            with self.subTest(item=item),self.assertRaises(ValueError): EVENTS.parse_print_events(item)

    def test_old_firmware_and_unknown_measurements(self):
        item=printer_payload();self.assertIsNone(API.parse_printer(item['printer'],item['status']).print_events)
        item['status']['job']['condition']='attention';item['status']['job']['progress_percent']=None
        item['status']['print_events']=journal()
        parsed=API.parse_printer(item['printer'],item['status'])
        self.assertEqual(parsed.condition,'attention');self.assertIsNone(parsed.progress_percent)
        item['status']['print_events']['events']=[{}]*9
        with self.assertRaises(API.PrintDeckInvalidResponseError): API.parse_printer(item['printer'],item['status'])

    def test_three_sources_are_not_deduplicated(self):
        bus=Mock();hass=SimpleNamespace(bus=bus)
        registry=SimpleNamespace(async_get_device=lambda **kwargs:SimpleNamespace(id='ha-printer'))
        item=printer_payload();base=API.parse_printer(item['printer'],item['status'])
        base=replace(base,stale=False,print_events=EVENTS.parse_print_events(journal()))
        with patch.object(HUB.dr,'async_get',return_value=registry):
            for number in range(3):
                hub=HUB.PrintDeckEventHub(hass);hub.enabled=True
                info=SimpleNamespace(device_id=f'printdeck-{number}')
                listener=Mock();unsubscribe=hub.listen(base.printer_id,listener)
                hub.consume(info,base)
                value=replace(base,print_events=EVENTS.parse_print_events(journal(1)))
                hub.consume(info,value);hub.consume(info,value)
                listener.assert_called_once();unsubscribe()
        payloads=[call.args[1] for call in bus.async_fire.call_args_list]
        self.assertEqual(len(payloads),3)
        self.assertEqual(len({value['routing_key'] for value in payloads}),3)
        self.assertEqual(len({value['event_id'] for value in payloads}),3)

    def test_events_do_not_wait_for_power_or_another_printer(self):
        state=STATE.PrintDeckMqttState(ROOT);state.ingest(ROOT+'/info',json.dumps(mqtt_info()),True,0)
        first=printer_payload();second=copy.deepcopy(first['printer']);second['id']=42
        def emit(suffix,payload):
            state.ingest(ROOT+'/'+suffix,json.dumps(dict(api_version='v1',_mqtt_generation='12345678-00000001',**payload)),False,1)
        emit('printers',dict(printers=[first['printer'],second]))
        state.ingest(ROOT+'/availability','online',False,1)
        first['status']['print_events']=journal(1)
        emit(f"printers/{first['printer']['id']}/status",dict(status=first['status']))
        self.assertIsNone(state.snapshot(1))
        self.assertEqual(len(state.event_printers(1)),1)
        self.assertEqual(state.event_printers(1)[0].print_events.sequence,1)

class EventLifecycleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = lifecycle.LifecycleTests.asyncSetUp
    start_pending = lifecycle.LifecycleTests.start_pending
    async def test_mqtt_journal_once_and_reconnect_quiet(self):
        coordinator,task=await self.start_pending();self.bus.complete();await task
        coordinator.events.enabled=True;self.hass.bus=Mock()
        registry=SimpleNamespace(async_get_device=lambda **kwargs:SimpleNamespace(id='ha-printer'))
        item=printer_payload();item['status']['connection']['stale']=False
        suffix=f"printers/{item['printer']['id']}/status"
        with patch.object(HUB.dr,'async_get',return_value=registry):
            for seq in (0,1,1):
                item['status']['print_events']=journal(seq)
                self.bus.emit(suffix,dict(api_version='v1',status=item['status']))
            self.assertEqual(self.hass.bus.async_fire.call_count,1)
            self.bus.emit('availability','offline')
            item['status']['print_events']=journal(2)
            self.bus.emit(suffix,dict(api_version='v1',status=item['status']))
            self.bus.emit('availability','online')
            self.assertEqual(self.hass.bus.async_fire.call_count,1)

class EventEntityTests(unittest.TestCase):
    def test_mqtt_event_entity_remains_available_without_aggregate_state(self):
        # Exercise the real event platform with only its HA base-class boundary stubbed.
        import importlib.util
        import sys
        from pathlib import Path
        from types import ModuleType
        component=Path(__file__).resolve().parents[1]/'custom_components/printdeck'
        native=ModuleType('homeassistant.components.event')
        native.EventEntity=type('EventEntity',(),{})
        native.EventEntityDescription=lambda **kwargs:SimpleNamespace(**kwargs)
        base=ModuleType('printdeck.entity')
        base.PrintDeckEntity=type('PrintDeckEntity',(),{})
        with patch.dict(sys.modules,{'homeassistant.components.event':native,'printdeck.entity':base}):
            spec=importlib.util.spec_from_file_location('printdeck.event_platform_test',component/'event.py')
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            entity=module.PrintDeckPrintEvent.__new__(module.PrintDeckPrintEvent)
            printer=SimpleNamespace(printer_id='1',stale=False,print_events=EVENTS.parse_print_events(journal()))
            entity._printer_id='1'
            entity.coordinator=SimpleNamespace(last_update_success=False,
                state=SimpleNamespace(event_printers=lambda now:(printer,)),
                events=SimpleNamespace(blocked=False))
            self.assertTrue(entity.available)
            entity.coordinator.events.blocked=True
            self.assertFalse(entity.available)
            entity.coordinator.events.blocked=False;printer.stale=True
            self.assertFalse(entity.available)
