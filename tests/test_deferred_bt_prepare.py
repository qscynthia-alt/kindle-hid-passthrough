#!/usr/bin/env python3
"""Tests for deferring Bluetooth preparation until actual Bluetooth use."""

import asyncio
import os
import sys
import types
from unittest.mock import Mock, patch


sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'kindle_hid_passthrough'))


class _Log:
    def info(self, *_args, **_kwargs):
        pass

    warning = error = success = info


def _stub(name, **members):
    module = types.ModuleType(name)
    for key, value in members.items():
        setattr(module, key, value)
    sys.modules[name] = module


_config = types.SimpleNamespace(
    get_all_devices=lambda: [],
    media_remote_enabled=False,
    log_file='unused.log',
)
_stub('button_mapper', register_all=lambda _devices: None)
_stub('api_server', PORT=8321, APIServer=object, RequestHandler=object)
_stub('bt_setup', chip=lambda: types.SimpleNamespace(ensure_powered=lambda: None),
      prepare_bt=lambda: True)
_stub('config', config=_config, get_version=lambda: 'test')
_stub('controller', DaemonController=object)
_stub('host', HIDHost=object)
_stub('logging_utils', errstr=str, log=_Log(), setup_daemon_logging=lambda _path: None)
_stub('power_monitor', PowerMonitor=object)
_stub('scanner', Scanner=object)

import daemon as daemon_module  # noqa: E402


def test_empty_daemon_wait_does_not_prepare_bluetooth():
    prepare = Mock(return_value=True)

    async def exercise():
        daemon = daemon_module.HIDDaemon()
        task = asyncio.create_task(daemon.run())
        await asyncio.sleep(0)

        assert daemon.running is True
        assert daemon.bt_prepared is False
        prepare.assert_not_called()

        daemon.running = False
        daemon._resume_event.set()
        await task

    with patch.object(daemon_module, 'prepare_bt', prepare), \
            patch.object(daemon_module.config, 'get_all_devices', return_value=[]), \
            patch.object(daemon_module.config, 'media_remote_enabled', False):
        asyncio.run(exercise())


def test_bluetooth_prepare_is_lazy_and_idempotent():
    prepare = Mock(return_value=True)
    daemon = daemon_module.HIDDaemon()

    with patch.object(daemon_module, 'prepare_bt', prepare):
        daemon.ensure_bt_prepared()
        daemon.ensure_bt_prepared()

    assert daemon.bt_prepared is True
    prepare.assert_called_once_with()


def test_failed_prepare_does_not_mark_bluetooth_ready():
    daemon = daemon_module.HIDDaemon()

    with patch.object(daemon_module, 'prepare_bt', return_value=False):
        try:
            daemon.ensure_bt_prepared()
        except RuntimeError as exc:
            assert str(exc) == 'Bluetooth hardware preparation failed'
        else:
            raise AssertionError('failed Bluetooth preparation should raise')

    assert daemon.bt_prepared is False


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    failed = 0
    for test in tests:
        try:
            test()
            print(f'ok   {test.__name__}')
        except Exception as exc:
            failed += 1
            print(f'FAIL {test.__name__}: {exc}')
    print(f'\n{len(tests) - failed}/{len(tests)} passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
