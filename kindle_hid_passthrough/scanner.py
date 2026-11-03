#!/usr/bin/env python3
"""Bluetooth scanner — discovers HID devices on BLE + Classic concurrently."""

import asyncio
from dataclasses import dataclass, field
from typing import List
from types import SimpleNamespace
from radio_attempt import RadioAttempt, RadioScan

from bumble.core import AdvertisingData, DeviceClass
from bumble.gatt import GATT_HUMAN_INTERFACE_DEVICE_SERVICE
from bumble.hci import (Address, HCI_Error, HCI_Remote_Name_Request_Command,
                        HCI_Inquiry_Cancel_Command, HCI_SyncCommand,
                        HCI_StatusAndAddressReturnParameters, metadata)


@HCI_SyncCommand.sync_command(HCI_StatusAndAddressReturnParameters)
@dataclass
class HCI_Remote_Name_Request_Cancel_Command(
        HCI_SyncCommand[HCI_StatusAndAddressReturnParameters]):
    """Bluetooth Core 7.1.20; command missing from Bumble 0.0.226."""

    bd_addr: Address = field(metadata=metadata(Address.parse_address))

from config import Protocol, config, clean_device_name
from logging_utils import log
from transport import create_bumble_device

__all__ = ['Scanner', 'DiscoveredDevice']


@dataclass
class DiscoveredDevice:
    """A discovered HID device."""
    address: str
    name: str
    protocol: Protocol
    rssi: int = -100

    def __str__(self) -> str:
        proto_tag = "[BLE]" if self.protocol == Protocol.BLE else "[Classic]"
        return f"{proto_tag} {self.name} ({self.address}) RSSI: {self.rssi}"


class Scanner:
    """Scans for HID devices across both BLE and Classic protocols.

    Uses a single Bumble Device instance to perform concurrent scanning.
    Falls back to sequential scanning if concurrent mode fails.
    """

    def __init__(self, transport_spec: str = None):
        """Initialize scanner.

        Args:
            transport_spec: HCI transport (default: from config)
        """
        self.transport_spec = transport_spec or config.transport
        self.transport = None
        self.device = None
        self.on_device_found = None  # Optional callback(DiscoveredDevice)
        self._radio_attempts = set()

    async def start(self):
        """Initialize the Bumble device."""
        self.transport, self.device = await create_bumble_device(self.transport_spec)

    async def cleanup(self):
        """Clean up resources."""
        owners = [o.task for o in self._radio_attempts]
        if owners:
            await asyncio.wait(owners)
        if self._radio_attempts:
            raise RuntimeError('Scan hardware state unresolved; transport close refused')
        hci_host = getattr(self.device, 'host', None)
        commands = list(getattr(hci_host, '_command_transactions', ()))
        if commands:
            await asyncio.wait(commands)
        if getattr(hci_host, '_command_fault', None):
            raise RuntimeError('Scan command response unconfirmed; transport close refused')
        if self.transport:
            await self.transport.close()
            self.transport = None
            from bt_setup import chip
            chip().on_transport_close()
        self.device = None

    async def _scan_window(self, event, listener, start, stop, duration, stop_event, detach_extra=None):
        # BLE scan and Classic inquiry may coexist; each retains its own stop
        # ACK, while cleanup waits for both before releasing the one transport.
        lease_host = SimpleNamespace(_radio_attempts=self._radio_attempts,
                                     _radio_lock=asyncio.Lock())
        await lease_host._radio_lock.acquire()
        pending = asyncio.get_running_loop().create_future()
        self.device.on(event, listener)
        def detach():
            self.device.remove_listener(event, listener)
            if detach_extra is not None:
                detach_extra()
        owner = RadioScan(lease_host, pending, start, stop,
                          detach)
        await owner.wait(duration, stop_event, caller_timeout=duration + 5.0)

    async def _remote_name(self, address):
        pending = asyncio.get_running_loop().create_future()
        target = Address(address)
        def success(peer, name):
            if peer == target and not pending.done():
                pending.set_result(name)
        def failure(peer, code):
            if peer == target and not pending.done():
                pending.set_exception(HCI_Error(code))
        self.device.on('remote_name', success)
        self.device.on('remote_name_failure', failure)
        def detach():
            self.device.remove_listener('remote_name', success)
            self.device.remove_listener('remote_name_failure', failure)
        lease_host = SimpleNamespace(_radio_attempts=self._radio_attempts,
                                     _radio_lock=asyncio.Lock())
        await lease_host._radio_lock.acquire()
        owner = RadioAttempt(lease_host, pending,
            lambda: self.device.host.send_async_command(HCI_Remote_Name_Request_Command(
                bd_addr=target, page_scan_repetition_mode=HCI_Remote_Name_Request_Command.R2,
                reserved=0, clock_offset=0)),
            lambda: self.device.host.send_sync_command(HCI_Remote_Name_Request_Cancel_Command(bd_addr=target)),
            detach, rejection_errors=(HCI_Error,))
        return await owner.wait(3.0, caller_timeout=8.0)

    async def scan(
        self,
        duration: float = 10.0,
        concurrent: bool = True,
        stop_event: asyncio.Event = None
    ) -> List[DiscoveredDevice]:
        """Scan for HID devices across both BLE and Classic.

        Args:
            duration: Scan duration in seconds (split between protocols if concurrent)
            concurrent: Try concurrent scanning (falls back to sequential if fails)
            stop_event: If set, scan stops early when this event is triggered

        Returns:
            List of discovered HID devices with protocol tags
        """
        if concurrent:
            try:
                return await self._scan_concurrent(duration, stop_event)
            except Exception as e:
                if self._radio_attempts:
                    raise
                log.warning(f"Concurrent scan failed ({e}), falling back to sequential")
                return await self._scan_sequential(duration, stop_event)
        else:
            return await self._scan_sequential(duration, stop_event)

    async def _scan_concurrent(self, duration: float, stop_event: asyncio.Event = None) -> List[DiscoveredDevice]:
        """Run BLE and Classic scans concurrently."""
        log.info(f"Scanning BLE + Classic concurrently ({duration}s)...")

        # Run both scans as concurrent tasks
        ble_task = asyncio.create_task(self._scan_ble(duration, stop_event))
        classic_task = asyncio.create_task(self._scan_classic(duration, stop_event))

        results = await asyncio.gather(ble_task, classic_task, return_exceptions=True)

        ble_devices = results[0] if not isinstance(results[0], Exception) else []
        classic_devices = results[1] if not isinstance(results[1], Exception) else []

        if isinstance(results[0], Exception):
            log.warning(f"BLE scan error: {results[0]}")
        if isinstance(results[1], Exception):
            log.warning(f"Classic scan error: {results[1]}")

        return self._merge_results(ble_devices, classic_devices)

    async def _scan_sequential(self, duration: float, stop_event: asyncio.Event = None) -> List[DiscoveredDevice]:
        """Run BLE and Classic scans sequentially."""
        half_duration = duration / 2.0

        log.info(f"Scanning BLE ({half_duration}s)...")
        ble_devices = await self._scan_ble(half_duration, stop_event)

        if stop_event and stop_event.is_set():
            return self._merge_results(ble_devices, [])

        log.info(f"Scanning Classic ({half_duration}s)...")
        classic_devices = await self._scan_classic(half_duration, stop_event)

        return self._merge_results(ble_devices, classic_devices)

    async def _interruptible_sleep(self, duration: float, stop_event: asyncio.Event = None):
        """Sleep for duration, but return early if stop_event is set."""
        if stop_event is None:
            await asyncio.sleep(duration)
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=duration)
            except asyncio.TimeoutError:
                pass

    async def _scan_ble(self, duration: float, stop_event: asyncio.Event = None) -> List[DiscoveredDevice]:
        """Scan for BLE HID devices."""
        devices_found: List[DiscoveredDevice] = []
        seen_addresses = set()

        def on_advertisement(advertisement):
            addr_str = str(advertisement.address)
            if addr_str in seen_addresses:
                return

            is_hid = False
            if hasattr(advertisement, 'data') and advertisement.data:
                try:
                    services = advertisement.data.get(
                        AdvertisingData.COMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS
                    ) or advertisement.data.get(
                        AdvertisingData.INCOMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS
                    )
                    if services:
                        for service_uuid in services:
                            if service_uuid == GATT_HUMAN_INTERFACE_DEVICE_SERVICE:
                                is_hid = True
                                break

                    if not is_hid:
                        appearance = advertisement.data.get(AdvertisingData.APPEARANCE)
                        if appearance is not None and (int(appearance) >> 6) == 0x0F:
                            is_hid = True
                except UnicodeDecodeError as e:
                    log.debug(f"Skipping malformed BLE advertisement from {addr_str}: {e}")
                    return

            if not is_hid:
                return
            seen_addresses.add(addr_str)

            name = 'Unknown'
            if hasattr(advertisement, 'data') and advertisement.data:
                try:
                    name = advertisement.data.get(AdvertisingData.COMPLETE_LOCAL_NAME) or \
                           advertisement.data.get(AdvertisingData.SHORTENED_LOCAL_NAME) or 'Unknown'
                except UnicodeDecodeError as e:
                    log.debug(f"Using Unknown name for malformed BLE advertisement from {addr_str}: {e}")
                    name = 'Unknown'
                name = clean_device_name(name) or 'Unknown'

            device = DiscoveredDevice(
                address=addr_str,
                name=name,
                protocol=Protocol.BLE,
                rssi=advertisement.rssi or -100
            )
            devices_found.append(device)
            log.info(f"  Found: {device}")
            if self.on_device_found:
                self.on_device_found(device)

        await self._scan_window('advertisement', on_advertisement,
                               lambda: self.device.start_scanning(filter_duplicates=True),
                               self.device.stop_scanning, duration, stop_event)

        return devices_found

    async def _scan_classic(self, duration: float, stop_event: asyncio.Event = None) -> List[DiscoveredDevice]:
        """Scan for Classic Bluetooth HID devices."""
        log.info(f"Starting Classic inquiry ({duration}s)...")
        devices_found: List[DiscoveredDevice] = []
        seen_addresses = set()

        def on_inquiry_result(address, class_of_device, eir_data, rssi):
            addr_str = str(address)
            if addr_str in seen_addresses:
                return
            seen_addresses.add(addr_str)

            # Check if HID device (Peripheral device class)
            is_hid = False
            major_class_name = "Unknown"
            try:
                _, major_class, _minor_class = DeviceClass.split_class_of_device(class_of_device)
                major_class_name = DeviceClass.major_device_class_name(major_class)
                is_hid = major_class_name == "Peripheral"
            except Exception:
                major_class = (class_of_device >> 8) & 0x1F
                is_hid = (major_class == 0x05)  # Peripheral
                major_class_name = f"0x{major_class:02X}"

            # Log ALL devices found, not just HID
            log.info(f"  Classic: {addr_str} CoD=0x{class_of_device:06X} ({major_class_name}) HID={is_hid}")

            if is_hid:
                name = 'Unknown'
                if eir_data:
                    try:
                        name_data = eir_data.get(0x09) or eir_data.get(0x08)
                        if name_data:
                            name = clean_device_name(name_data) or 'Unknown'
                            raw = (name_data if isinstance(name_data, bytes)
                                   else str(name_data).encode('utf-8', errors='replace'))
                            if name != raw.decode('utf-8', errors='ignore').strip():
                                log.info(f"  Cleaned EIR name for {addr_str}: {raw.hex()}")
                    except Exception:
                        pass

                device = DiscoveredDevice(
                    address=addr_str,
                    name=name,
                    protocol=Protocol.CLASSIC,
                    rssi=rssi or -100
                )
                devices_found.append(device)
                log.info(f"  Found: {device}")
                if self.on_device_found:
                    self.on_device_found(device)

        completed = asyncio.Event()
        def on_complete():
            completed.set()
        self.device.on('inquiry_complete', on_complete)
        async def start():
            self.device.auto_restart_inquiry = False
            await self.device.start_discovery(auto_restart=False)
        async def stop():
            self.device.auto_restart_inquiry = False
            if not completed.is_set():
                try:
                    # Do not rely on Device.discovering: a partial start can
                    # leave it false even after Inquiry reached the controller.
                    await self.device.host.send_sync_command(HCI_Inquiry_Cancel_Command())
                except HCI_Error:
                    await completed.wait()
            self.device.auto_restart_inquiry = False
            self.device.discovering = False
        await self._scan_window('inquiry_result', on_inquiry_result,
                               start, stop, duration, stop_event,
                               detach_extra=lambda: self.device.remove_listener('inquiry_complete', on_complete))

        # Get names for unknown devices
        for dev in devices_found:
            if dev.name == 'Unknown':
                try:
                    name = await self._remote_name(dev.address)
                    if name:
                        dev.name = name
                except Exception:
                    pass

        return devices_found

    def _merge_results(
        self,
        ble_devices: List[DiscoveredDevice],
        classic_devices: List[DiscoveredDevice]
    ) -> List[DiscoveredDevice]:
        """Merge and deduplicate scan results.

        Some devices may advertise on both BLE and Classic. In that case,
        we keep both entries since the user may want to choose the protocol.

        Results are sorted by RSSI (strongest signal first).
        """
        all_devices = ble_devices + classic_devices

        # Sort by RSSI (strongest first)
        all_devices.sort(key=lambda d: d.rssi, reverse=True)

        log.success(f"Found {len(ble_devices)} BLE + {len(classic_devices)} Classic = {len(all_devices)} HID devices")
        return all_devices
