#!/usr/bin/env python3
"""MTK preparation must not open /dev/stpbt merely to test availability."""

import os
import sys
import types
from unittest.mock import patch

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'kindle_hid_passthrough'))


class _Log:
    def info(self, *_args, **_kwargs):
        pass

    warning = error = info


config_module = types.ModuleType('config')
config_module.config = types.SimpleNamespace(
    bt_settle_time=0,
    bt_module_patterns=None,
)
sys.modules['config'] = config_module

logging_module = types.ModuleType('logging_utils')
logging_module.log = _Log()
sys.modules['logging_utils'] = logging_module

import bt_mtk


def test_prepare_with_no_holder_never_opens_device():
    kindle = type('Kindle', (), {
        'device_path': '/dev/stpbt',
        'kernel_module': 'wmt_cdev_bt.ko',
    })()

    with patch.object(bt_mtk, '_find_bt_module', return_value='/fake/wmt_cdev_bt.ko'), \
            patch.object(bt_mtk, '_is_module_loaded', return_value=True), \
            patch.object(bt_mtk.os.path, 'exists', return_value=True), \
            patch.object(bt_mtk, '_find_holders', return_value=[]), \
            patch.object(bt_mtk.os, 'open') as device_open:
        assert bt_mtk.MtkChip(kindle).prepare() is True

    device_open.assert_not_called()
