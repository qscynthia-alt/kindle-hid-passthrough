#!/usr/bin/env python3
"""BLE HID handler mixin for HIDHost."""

import asyncio
import time

from bumble.core import AdvertisingData, BT_LE_TRANSPORT, InvalidStateError
from bumble.device import Device, Peer
from bumble.gatt import (
    GATT_BATTERY_LEVEL_CHARACTERISTIC,
    GATT_BATTERY_SERVICE,
    GATT_DEVICE_NAME_CHARACTERISTIC,
    GATT_GENERIC_ACCESS_SERVICE,
    GATT_HID_CONTROL_POINT_CHARACTERISTIC,
    GATT_HUMAN_INTERFACE_DEVICE_SERVICE,
    GATT_PROTOCOL_MODE_CHARACTERISTIC,
    GATT_REPORT_CHARACTERISTIC,
    GATT_REPORT_MAP_CHARACTERISTIC,
    GATT_REPORT_REFERENCE_DESCRIPTOR,
)
from bumble.hci import (
    Address,
    HCI_Error,
    HCI_LE_Add_Device_To_Filter_Accept_List_Command,
    HCI_LE_Clear_Filter_Accept_List_Command,
    HCI_LE_Create_Connection_Cancel_Command,
    HCI_LE_Create_Connection_Command,
    OwnAddressType,
    Role,
)

from config import Protocol, config, normalize_addr, clean_device_name
from radio_attempt import RadioAttempt, RadioScan
from logging_utils import log

HID_REPORT_TYPE_INPUT = 1
HID_REPORT_TYPE_OUTPUT = 2

# Re-read the battery level this often, for devices that never notify.
BATTERY_POLL_INTERVAL = 300


class BLEMixin:
    """BLE methods for HIDHost."""

    BLE_INIT_WINDOW = 18.0
    BLE_SCAN_WINDOW = 8.0

    def _mark_ble(self, stage):
        # Replaced atomically; read-only status never walks /proc or task locals.
        self._ble_progress = {"stage": stage, "at": time.monotonic()}

    def ble_diagnostics(self):
        progress = dict(getattr(self, '_ble_progress', {}))
        stamp = progress.pop('at', None)
        progress['age_seconds'] = round(max(0.0, time.monotonic() - stamp), 1) if stamp is not None else None
        task = getattr(self, '_ble_handler_task', None)
        progress['task_done'] = task.done() if task is not None else None
        progress['task_cancelled'] = task.cancelled() if task is not None else None
        chain = []
        current = task.get_coro() if task is not None else None
        for _ in range(8):
            if current is None:
                break
            frame = getattr(current, 'cr_frame', None) or getattr(current, 'gi_frame', None)
            if frame is not None:
                chain.append({"function": frame.f_code.co_name[:80], "line": frame.f_lineno})
            current = getattr(current, 'cr_await', None) or getattr(current, 'gi_yieldfrom', None)
        progress['await_chain'] = chain
        progress['recent_link_events'] = list(getattr(self, '_ble_link_events', []))
        progress['radio_owners'] = [
            {'state': owner.state, 'caller_stopped': owner.abandoned,
             'result_received': owner.pending.done() and not owner.pending.cancelled()}
            for owner in tuple(getattr(self, '_radio_attempts', ()))]
        hci_host = getattr(getattr(self, 'device', None), 'host', None)
        progress['hci_commands_pending'] = sum(not t.done() for t in tuple(getattr(hci_host, '_command_transactions', ())))
        progress['hci_command_fault'] = getattr(hci_host, '_command_fault', None)
        return progress

    async def _run_ble_handler(self):
        """Handle BLE connections."""
        self._ble_handler_task = asyncio.current_task()
        self._mark_ble('handler_started')
        has_known = any(dev.address != '*' for dev in self.ble_devices)
        has_wildcard = any(dev.address == '*' for dev in self.ble_devices)

        if has_known:
            await self._run_ble_accept_list_handler()
        elif has_wildcard:
            await self._run_ble_scan_handler(set())

    def retry_ble_connection(self, address):
        """Queue one targeted pass on the existing BLE owner, never reset it."""
        address = normalize_addr(address)
        if address not in {normalize_addr(d.address) for d in self.ble_devices}:
            return False
        task = getattr(self, '_ble_handler_task', None)
        if task is None or task.done():
            return False
        if hasattr(self, '_request_manual_peer'):
            self._request_manual_peer(address, 'ble')
        self._ble_preferred_address = address
        self._ble_manual_priority_until = time.monotonic() + 45.0
        classic = getattr(self, '_classic_retry_event', None)
        if classic is not None:
            classic.set()
        event = getattr(self, '_ble_retry_event', None)
        if event is not None:
            event.set()
        return True

    def _ble_missing_addresses(self):
        """Configured BLE addresses without a live session."""
        return [normalize_addr(d.address) for d in self.ble_devices
                if d.address != '*' and normalize_addr(d.address) not in self.sessions
                and (not hasattr(self, '_reconnect_allowed') or self._reconnect_allowed(d.address, 'ble'))
                and getattr(self, 'connection_policy', {}).get((normalize_addr(d.address), 'ble')) != 'disconnect']

    async def _seed_accept_list(self, missing):
        """Load the filter accept list with every address we may connect to."""
        if self.device.address_resolution_offload:
            await self._set_device_privacy_modes()
        await self.device.send_command(
            HCI_LE_Clear_Filter_Accept_List_Command(), check_result=True)

        known = set(missing)

        for addr_str in sorted(known):
            target = Address(addr_str)
            known_type = self._keystore_address_types.get(addr_str)
            if known_type is not None:
                entry_types = [known_type & 1]
            else:
                entry_types = [Address.PUBLIC_DEVICE_ADDRESS, Address.RANDOM_DEVICE_ADDRESS]
            for entry_type in entry_types:
                await self.device.send_command(
                    HCI_LE_Add_Device_To_Filter_Accept_List_Command(
                        address_type=entry_type,
                        address=target,
                    ), check_result=True)
        return known

    async def _run_ble_accept_list_handler(self):
        """Connect to configured BLE devices using the filter accept list."""
        self._ble_retry_event = asyncio.Event()
        log.info("[BLE] Accept-list handler running")

        while True:
            manual = False
            try:
                self._mark_ble('checking_saved_devices')
                missing = self._ble_missing_addresses()
                if not missing:
                    self._mark_ble('all_saved_devices_connected')
                    try:
                        await asyncio.wait_for(self._ble_retry_event.wait(), self.ACTIVE_RETRY_INTERVAL)
                    except asyncio.TimeoutError:
                        pass
                    self._ble_retry_event.clear()
                    continue

                self._mark_ble('seeding_accept_list')
                # Policy may have changed while waiting on a physical owner.
                missing = self._ble_missing_addresses()
                if not missing:
                    continue
                known = set(missing)

                matched_dev = None
                match_kind = None
                preferred = getattr(self, '_ble_preferred_address', None)
                self._ble_preferred_address = None
                if preferred in missing:
                    manual = True
                    self._mark_ble('manual_target_attempt')
                    known_type = self._keystore_address_types.get(preferred)
                    if known_type is not None:
                        connection = await self._ble_initiate(
                            self.BLE_INIT_WINDOW, peer=Address(preferred, known_type & 1))
                    else:
                        connection = await self._ble_initiate(self.BLE_INIT_WINDOW)
                else:
                    connection = await self._ble_initiate(self.BLE_INIT_WINDOW)

                if connection is None and getattr(self, '_ble_preferred_address', None) in missing:
                    # A user request arrived during the old bounded attempt.
                    # Consume it before another scan or background page.
                    continue

                if connection is None:
                    match = await self._ble_scan_for_rotated(known, self.BLE_SCAN_WINDOW)
                    if match:
                        target_address, matched_dev, match_kind = match
                        connection = await self._ble_initiate(
                            config.connect_timeout, peer=target_address)

                if connection is None:
                    continue

                self._mark_ble('admitting_connection')
                self._admit_ble_connection(connection, matched_dev, match_kind)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"[BLE] Connection failed: {e}")
                await asyncio.sleep(2.0)
            finally:
                if manual and getattr(self, '_ble_preferred_address', None) is None:
                    self._ble_manual_priority_until = 0.0

    def _observe_ble_link(self, connection):
        # Fixed-size metadata only: no HID payload, keys, process scan or timer.
        def record(event, error=None):
            params = getattr(connection, 'parameters', None)
            item = {'event': event, 'at': round(time.monotonic(), 3),
                    'address': normalize_addr(str(connection.peer_address)),
                    'encrypted': bool(connection.is_encrypted),
                    'reports': getattr(connection, '_hid_report_count', 0)}
            if error is not None:
                item['code'] = int(error)
            if params is not None:
                for name in ('connection_interval', 'peripheral_latency', 'supervision_timeout'):
                    value = getattr(params, name, None)
                    if isinstance(value, (int, float)):
                        item[name] = value
            last_input = getattr(connection, '_hid_last_input_at', None)
            item['last_input_age_seconds'] = round(max(0, time.monotonic() - last_input), 3) if last_input is not None else None
            item['classic_active_address'] = getattr(self, '_classic_active_address', None)
            hci = getattr(getattr(self, 'device', None), 'host', None)
            item['hci_commands_pending'] = len(getattr(hci, '_command_transactions', ()))
            item['hci_fault'] = getattr(hci, '_command_fault', None)
            self._ble_link_events = (getattr(self, '_ble_link_events', []) + [item])[-64:]
            if event == 'disconnected':
                log.warning(f'[BLE] Link ended: last_input_age={item["last_input_age_seconds"]}s '
                            f'reports={item["reports"]} classic_active={item["classic_active_address"]} '
                            f'hci_pending={item["hci_commands_pending"]} hci_fault={item["hci_fault"]}')
        connection.on('connection_encryption_change', lambda: record('encryption_changed'))
        connection.on('connection_encryption_failure', lambda code: record('encryption_failed', code))
        connection.on('connection_parameters_update', lambda: record('parameters_changed'))
        connection.on('connection_parameters_update_failure', lambda code: record('parameters_failed', code))
        connection.on('disconnection', lambda code: record('disconnected', code))
        record('connected')

    def _admit_ble_connection(self, connection, matched_dev=None, match_kind=None):
        """Create a session for a new BLE connection, dropping duplicates."""
        addr = normalize_addr(str(connection.peer_address))
        identity = normalize_addr(matched_dev.address) if matched_dev is not None else addr
        if hasattr(self, '_reconnect_allowed') and not self._reconnect_allowed(identity, 'ble'):
            self._track_task(asyncio.create_task(self._reject_connection(connection)))
            return

        # A connect started before /remove still lands here; without this the
        # session comes back and re-pairs, so the app keeps showing the device.
        allowed = {normalize_addr(d.address) for d in self.ble_devices} | self._keystore_addresses
        if match_kind is None and '*' not in allowed and addr not in allowed:
            log.warning(f"[BLE] Rejecting {addr} (not allowed)")
            self._track_task(asyncio.create_task(self._reject_connection(connection)))
            return

        if getattr(self, 'connection_policy', {}).get((addr, 'ble')) == 'disconnect':
            self._track_task(asyncio.create_task(self._reject_connection(connection)))
            return
        old = self.sessions.get(addr)
        if old is not None and old.is_alive():
            log.info(f"[BLE] Duplicate connection from {addr}, dropping")
            self._track_task(asyncio.create_task(self._reject_connection(connection)))
            return

        log.info(f"[BLE] Device connected: {self._format_device(addr)}")
        self._observe_ble_link(connection)
        session = self._new_session(addr, Protocol.BLE, connection)
        session.reconnect_identity = identity
        policy = getattr(self, 'reconnect_policy', None)
        if policy is not None:
            session.reconnect_generation = policy.generation(identity, 'ble')
        session.peer = Peer(connection)
        self._register_session(session)
        session.setup_task = self._track_task(asyncio.create_task(
            self._run_session_setup(
                session,
                self._setup_ble_session(session, matched_dev, match_kind, old))))

    async def _setup_ble_session(self, session, matched_dev=None, match_kind=None, old=None):
        """Pair or restore bonding, then bring up HID for one session."""
        if old is not None:
            await self._teardown_session(old)

        await self._ble_restore_or_pair(session)

        if match_kind == 'name' and matched_dev is not None:
            self._save_rotated_address(matched_dev, session.address)
            self._parse_devices()

        self._load_cached_descriptor(session)
        await self._setup_ble_hid(session)
        log.success(f"[BLE] {self._format_device(session.address)} receiving HID reports")

    async def _read_ble_battery(self, session):
        """Subscribe to Battery Level (0x2A19) and read it once, if present."""
        peer = session.peer
        service = next((s for s in peer.services if s.uuid == GATT_BATTERY_SERVICE), None)
        if service is None:
            log.info(f"[BLE] No Battery Service on {session.address}, has "
                     f"{[str(s.uuid) for s in peer.services]}")
            return
        try:
            await peer.discover_characteristics(service=service)
            char = next((c for c in service.characteristics
                         if c.uuid == GATT_BATTERY_LEVEL_CHARACTERISTIC), None)
            if char is None:
                return
            session.battery_char = char
            try:
                await peer.subscribe(char, lambda value: self._update_battery(session, value))
            except Exception as e:
                log.debug(f"[BLE] Battery notify unavailable for {session.address}: {e}")
            self._update_battery(session, await peer.read_value(char))
        except Exception as e:
            log.warning(f"[BLE] Battery read failed for {session.address}: {e}")

    def _update_battery(self, session, value):
        """Store a Battery Level reading, logging only when it moves."""
        if not value or not 0 <= value[0] <= 100:
            return
        session.battery_updated = time.time()
        if value[0] != session.battery_level:
            session.battery_level = value[0]
            log.info(f"[BLE] {self._format_device(session.address)} battery: {value[0]}%")

    async def _run_ble_battery_poller(self):
        """Refresh the battery of devices that answer reads but never notify."""
        while True:
            await asyncio.sleep(BATTERY_POLL_INTERVAL)
            for session in list(self.sessions.values()):
                if session.battery_char is None or not session.is_alive():
                    continue
                try:
                    self._update_battery(
                        session, await session.peer.read_value(session.battery_char))
                except Exception as e:
                    log.warning(f"[BLE] Battery poll failed for {session.address}: {e}")

    async def _ble_initiate(self, window: float, peer: Address = None, pairing=False):
        """Legacy create-connection to `peer`, or to the accept list when
        None. Returns the connection, or None on window timeout."""
        pending = asyncio.get_running_loop().create_future()

        def on_connection(connection):
            if (connection.transport == BT_LE_TRANSPORT
                    and connection.role == Role.CENTRAL and not pending.done()):
                pending.set_result(connection)

        def on_failure(error):
            if getattr(error, 'transport', BT_LE_TRANSPORT) != BT_LE_TRANSPORT:
                return
            if not pending.done():
                pending.set_exception(error)

        def consume_exception(future):
            if not future.cancelled():
                future.exception()
        pending.add_done_callback(consume_exception)

        self._mark_ble('waiting_radio_lock')
        await self._radio_lock.acquire()
        if not pairing and hasattr(self, '_ble_missing_addresses'):
            missing = self._ble_missing_addresses()
            if not missing:
                self._radio_lock.release()
                return None
            if peer is None:
                try:
                    await self._seed_accept_list(missing)
                except BaseException:
                    self._radio_lock.release()
                    raise
        self.device.on(Device.EVENT_CONNECTION, on_connection)
        self.device.on(Device.EVENT_CONNECTION_FAILURE, on_failure)

        self.device.connect_own_address_type = OwnAddressType.PUBLIC
        self.device.le_connecting = True

        def detach():
            self.device.le_connecting = False
            self.device.remove_listener(Device.EVENT_CONNECTION, on_connection)
            self.device.remove_listener(Device.EVENT_CONNECTION_FAILURE, on_failure)

        async def create():
            self._mark_ble('sending_create_connection')
            await self.device.host.send_command(
                HCI_LE_Create_Connection_Command(
                    le_scan_interval=96,
                    le_scan_window=96,
                    initiator_filter_policy=0 if peer is not None else 1,
                    peer_address_type=peer.address_type if peer is not None else 0,
                    peer_address=peer if peer is not None else Address.ANY,
                    own_address_type=OwnAddressType.PUBLIC,
                    connection_interval_min=12,
                    connection_interval_max=24,
                    max_latency=0,
                    supervision_timeout=500,
                    min_ce_length=0,
                    max_ce_length=0,
                ), check_result=True)

        owner = RadioAttempt(
            self, pending, create,
            lambda: self.device.host.send_command(HCI_LE_Create_Connection_Cancel_Command(), check_result=True),
            detach, abandon=(lambda conn: self._retain_pair_connection(conn, Protocol.BLE)) if pairing else self._admit_ble_connection,
            rejection_errors=(HCI_Error,))
        return await owner.wait(window)

    async def _ble_scan_for_rotated(self, known: set, window: float):
        """Scan for bonded devices advertising, including from a rotated
        address. Returns (address, DeviceConfig or None, kind) or None."""
        rotated = asyncio.get_running_loop().create_future()

        def on_advertisement(advertisement):
            if rotated.done():
                return
            match = self._match_rotated_ble_device(advertisement, known)
            if match:
                rotated.set_result((advertisement.address,) + match)

        self._mark_ble('waiting_radio_lock')
        await self._radio_lock.acquire()
        self.device.on('advertisement', on_advertisement)

        async def start():
            self._mark_ble('rotation_scan')
            await self.device.start_scanning(
                legacy=True,
                own_address_type=OwnAddressType.PUBLIC,
                filter_duplicates=True,
            )

        def detach():
            self.device.remove_listener('advertisement', on_advertisement)

        return await RadioScan(self, rotated, start,
                               lambda: self.device.stop_scanning(legacy=True),
                               detach).wait(window)

    def _match_rotated_ble_device(self, advertisement, known: set):
        """Match an advertisement by known address, IRK resolution, or
        device name. Returns (DeviceConfig or None, kind) or None."""
        address = advertisement.address
        addr_norm = normalize_addr(str(address))
        if addr_norm in self.sessions:
            return None
        if addr_norm in known:
            dev = next((d for d in self.ble_devices if d.address == addr_norm), None)
            return (dev, 'known')

        if address.is_resolvable and self.device.address_resolver:
            resolved = self.device.address_resolver.resolve(address)
            if resolved:
                resolved_norm = normalize_addr(str(resolved))
                if resolved_norm in known:
                    dev = next((d for d in self.ble_devices if d.address == resolved_norm), None)
                    log.info(f"[BLE] Resolved {addr_norm} to bonded device "
                             f"{self._format_device(resolved_norm)}")
                    return (dev, 'irk')

        # Names are display labels, not an identity for the persisted last peer.
        # A changed address needs the peer's IRK resolution above.
        if getattr(self, 'reconnect_policy', None) is not None:
            return None
        try:
            name = advertisement.data.get(AdvertisingData.COMPLETE_LOCAL_NAME) or \
                advertisement.data.get(AdvertisingData.SHORTENED_LOCAL_NAME)
        except UnicodeDecodeError as e:
            log.debug(f"[BLE] Ignoring malformed advertisement from {addr_norm}: {e}")
            return None
        if isinstance(name, bytes):
            name = clean_device_name(name)
        if name:
            dev = next((d for d in self.ble_devices
                        if d.name == name and d.address in known and d.address not in self.sessions), None)
            if dev:
                log.info(f"[BLE] {name} advertising from new address {addr_norm}")
                return (dev, 'name')

        return None

    def _save_rotated_address(self, dev, new_addr: str):
        """Save the new address and drop the device's stale entries."""
        config.add_device(new_addr, Protocol.BLE, dev.name)
        if dev.address != new_addr:
            config.remove_device(dev.address)

    async def _run_ble_scan_handler(self, target_addresses: set):
        """Fallback BLE handler using active scanning for discovery."""
        log.info("[BLE] Scanning for devices...")
        self._ble_retry_event = asyncio.Event()

        while True:
            policy = getattr(self, 'reconnect_policy', None)
            if policy is not None:
                selected = policy.snapshot()
                target = selected['manual_target'] or selected['last_successful']
                if not target or target['protocol'] != 'ble' or target['address'] in self.sessions:
                    try:
                        await asyncio.wait_for(self._ble_retry_event.wait(), 2.0)
                    except asyncio.TimeoutError:
                        pass
                    self._ble_retry_event.clear()
                    continue
                target_addresses = {target['address']}
            found_device = None
            found = asyncio.get_running_loop().create_future()

            def on_advertisement(advertisement):
                nonlocal found_device
                if found_device is not None:
                    return

                addr = normalize_addr(str(advertisement.address))
                if addr in self.sessions:
                    return
                if not target_addresses or addr in target_addresses:
                    found_device = advertisement
                    if not found.done():
                        found.set_result(advertisement)
                    log.info(f"[BLE] Found target: {addr}")

            await self._radio_lock.acquire()
            self.device.on('advertisement', on_advertisement)
            await RadioScan(self, found, self.device.start_scanning,
                            self.device.stop_scanning,
                            lambda: self.device.remove_listener('advertisement', on_advertisement)).wait(10.0)

            if found_device:
                max_attempts = 2
                for attempt in range(1, max_attempts + 1):
                    try:
                        log.info(f"[BLE] Connecting to {found_device.address} (Attempt {attempt}/{max_attempts})...")
                        connection = await self._ble_initiate(
                            config.connect_timeout, peer=found_device.address)
                        if connection is None:
                            continue
                        self._admit_ble_connection(connection)
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning(f"[BLE] Connect attempt {attempt} failed: {e}")
                        if attempt < max_attempts:
                            await asyncio.sleep(2.0)

            await asyncio.sleep(3.0)

    async def _ble_restore_or_pair(self, session):
        """Restore BLE bonding or initiate new pairing."""
        connection = session.connection
        if connection.is_encrypted:
            log.info("[BLE] Connection already encrypted")
            return

        if self.device.keystore:
            try:
                keys = await self.device.keystore.get(str(connection.peer_address))
                if keys:
                    log.info("[BLE] Restoring bonding...")
                    await connection.encrypt()
                    log.success("[BLE] Bonding restored")
                    return
            except Exception as e:
                log.warning(f"[BLE] Bonding restore failed: {e}")

        log.info("[BLE] Initiating pairing...")
        await connection.pair()
        log.success("[BLE] Pairing complete")

    async def _setup_ble_hid(self, session):
        """Discover reports, create UHID, subscribe. Common to connect and post-pair."""
        if not session.hid_reports:
            await self._discover_ble_hid_service(session, process_reports=True)
        if not session.report_map:
            raise InvalidStateError("[BLE] No report descriptor available")
        self._create_uhid_device(session)
        await self._subscribe_to_ble_reports(session)
        await self._ble_activate_hid_service(session)
        self._track_task(asyncio.create_task(self._read_ble_battery(session)))

    async def _read_ble_device_name(self, session):
        """Read BLE device name from Generic Access Service."""
        try:
            for service in session.peer.services:
                if service.uuid == GATT_GENERIC_ACCESS_SERVICE:
                    await session.peer.discover_characteristics(service=service)
                    for char in service.characteristics:
                        if char.uuid == GATT_DEVICE_NAME_CHARACTERISTIC:
                            value = await session.peer.read_value(char)
                            session.name = clean_device_name(bytes(value))
                            log.info(f"[BLE] Device name: {session.name}")
                            return
        except Exception as e:
            log.warning(f"[BLE] Could not read device name: {e}")

    async def _process_ble_report_char(self, session, char):
        """Process a BLE Report characteristic."""
        await session.peer.discover_descriptors(characteristic=char)

        report_id = 0
        report_type = HID_REPORT_TYPE_INPUT

        for desc in char.descriptors:
            if desc.type == GATT_REPORT_REFERENCE_DESCRIPTOR:
                try:
                    ref = await session.peer.read_value(desc)
                    if len(ref) >= 2:
                        report_id = ref[0]
                        report_type = ref[1]
                except Exception:
                    pass

        if report_type == HID_REPORT_TYPE_INPUT:
            session.hid_reports.append((report_id, char))
            log.info(f"[BLE] Found input report {report_id}")
        elif report_type == HID_REPORT_TYPE_OUTPUT:
            session.output_reports[report_id] = char
            log.info(f"[BLE] Found output report {report_id}")

    async def _subscribe_to_ble_reports(self, session):
        """Subscribe to BLE HID input report notifications."""
        for report_id, char in session.hid_reports:
            try:
                def make_callback(rid):
                    return lambda value: self._on_ble_hid_report(session, value, rid)

                await session.peer.subscribe(char, make_callback(report_id))
                log.success(f"[BLE] Subscribed to report {report_id}")
            except Exception as e:
                log.warning(f"[BLE] Failed to subscribe to report {report_id}: {e}")

    async def _ble_activate_hid_service(self, session):
        """Write Exit Suspend to HID Control Point and force Report Protocol Mode."""
        peer = session.peer
        if not peer:
            log.warning("[BLE] No peer for HID activation")
            return

        hid_services = [s for s in peer.services if s.uuid == GATT_HUMAN_INTERFACE_DEVICE_SERVICE]
        if not hid_services:
            log.warning("[BLE] No HID service found for activation")
            return

        hid_service = hid_services[0]
        if not hid_service.characteristics:
            log.info("[BLE] Discovering characteristics for HID activation...")
            await peer.discover_characteristics(service=hid_service)

        found_cp = False
        for char in hid_service.characteristics:
            if char.uuid == GATT_HID_CONTROL_POINT_CHARACTERISTIC:
                found_cp = True
                try:
                    await peer.write_value(char, bytes([0x01]), with_response=False)
                    log.info("[BLE] Wrote Exit Suspend to HID Control Point")
                except Exception as e:
                    log.warning(f"[BLE] Failed to write HID Control Point: {e}")

            elif char.uuid == GATT_PROTOCOL_MODE_CHARACTERISTIC:
                try:
                    value = await peer.read_value(char)
                    mode = "Report" if bytes(value) == b'\x01' else "Boot"
                    log.info(f"[BLE] Protocol Mode: {mode}")
                    if bytes(value) != b'\x01':
                        await peer.write_value(char, bytes([0x01]), with_response=False)
                        log.info("[BLE] Forced Report Protocol Mode")
                except Exception as e:
                    log.warning(f"[BLE] Protocol Mode read/write failed: {e}")

        if not found_cp:
            log.info(f"[BLE] No HID Control Point characteristic (found {len(hid_service.characteristics)} chars)")

    def _on_ble_hid_report(self, session, value, report_id):
        """Handle BLE HID report."""
        if session.connection is not None:
            session.connection._hid_report_count = getattr(session.connection, '_hid_report_count', 0) + 1
        prefix = bytes([report_id]) if report_id else b''
        self._forward_report(session, prefix + bytes(value))

    async def _pair_ble(self, address: str) -> bool:
        """Pair with a BLE device."""
        log.info(f"[BLE] Pairing with {address}...")

        target = Address(address)
        try:
            connection = await self._ble_initiate(config.connect_timeout, peer=target, pairing=True)
            if connection is None:
                return False
        except Exception as e:
            log.error(f"[BLE] Connection failed: {e}")
            return False

        session = self._new_session(normalize_addr(address), Protocol.BLE, connection)
        session.peer = Peer(connection)
        self._pairing_session = session
        log.success(f"[BLE] Connected to {address}")

        try:
            log.info("[BLE] Initiating pairing...")
            await connection.pair()
            log.success("[BLE] Pairing complete!")

            if not connection.is_encrypted:
                raise InvalidStateError("[BLE] Link not encrypted after pairing")

            await self._discover_ble_hid_service(session)

            return True
        except Exception as e:
            log.error(f"[BLE] Pairing failed: {e}")
            await session.cleanup()
            self._pairing_session = None
            return False

    async def _discover_ble_hid_service(self, session, process_reports: bool = False):
        """Discover BLE GATT HID service and cache descriptor."""
        peer = session.peer
        await peer.discover_services()

        if not session.name:
            await self._read_ble_device_name(session)

        hid_services = [s for s in peer.services if s.uuid == GATT_HUMAN_INTERFACE_DEVICE_SERVICE]
        if not hid_services:
            if process_reports:
                raise InvalidStateError("[BLE] HID service not found")
            log.warning("[BLE] HID service not found")
            return

        hid_service = hid_services[0]
        log.success("[BLE] Found HID service")

        await peer.discover_characteristics(service=hid_service)

        for char in hid_service.characteristics:
            if char.uuid == GATT_REPORT_MAP_CHARACTERISTIC and not session.report_map:
                try:
                    value = await peer.read_value(char)
                    session.report_map = bytes(value)
                    log.success(f"[BLE] Got descriptor: {len(session.report_map)} bytes")

                    self.device_cache.save(session.address, {
                        'report_map': session.report_map.hex(),
                        'device_name': session.name
                    })
                except Exception as e:
                    log.warning(f"[BLE] Failed to read report map: {e}")

            elif process_reports and char.uuid == GATT_REPORT_CHARACTERISTIC:
                await self._process_ble_report_char(session, char)

    async def _continue_ble_after_pairing(self, session):
        """Continue BLE connection after pairing."""
        if not session.peer:
            session.peer = Peer(session.connection)
        await self._ble_restore_or_pair(session)
        await self._setup_ble_hid(session)
