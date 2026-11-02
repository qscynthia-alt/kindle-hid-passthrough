"""Finite connection intents. No I/O, radio resets, tasks or background timers."""
from collections import OrderedDict
from copy import deepcopy
import re
import threading
import time
import uuid

ACTIVE = frozenset({'waiting'})
TERMINAL = frozenset({'done', 'refused', 'failed', 'timed_out', 'cancelled', 'instance_changed'})


class ConnectionStateMachine:
    SCHEMA = 2
    LIMIT = 32
    ACTIVE_LIMIT = 16
    ALIAS_LIMIT = 128

    def __init__(self, clock=time.monotonic, instance_id=None):
        self.clock = clock
        self.instance_id = instance_id or uuid.uuid4().hex
        self.operations = OrderedDict()
        self.requests = OrderedDict()
        self.desired = {}
        self.lock = threading.RLock()
        self.sequence = 0

    @staticmethod
    def _target(address, protocol):
        if not isinstance(address, str) or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', address):
            raise ValueError('Invalid saved-device address')
        if protocol not in ('ble', 'classic'):
            raise ValueError('Invalid protocol')
        return address.upper(), protocol

    @staticmethod
    def _identifier(value):
        return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,96}', value) is not None

    def _finish(self, op, phase, reason):
        if op['phase'] in ACTIVE:
            op.update(phase=phase, reason=reason, finished=self.clock())

    def _saved(self, status, target):
        return any((str(d.get('address', '')).upper(), d.get('protocol')) == target for d in status.get('devices', []))

    def _connection(self, status, target):
        return next((d for d in status.get('connections', []) if
                     (str(d.get('address', '')).upper(), d.get('protocol')) == target), None)

    def _reconcile(self, status):
        now = self.clock()
        saved = {(str(d.get('address', '')).upper(), d.get('protocol')) for d in status.get('devices', [])}
        self.desired = {target: value for target, value in self.desired.items() if target in saved}
        for op in self.operations.values():
            if op['phase'] not in ACTIVE:
                continue
            target = (op['address'], op['protocol'])
            link = self._connection(status, target)
            if not self._saved(status, target):
                self._finish(op, 'failed', 'device_removed')
            elif status.get('worker_fault'):
                self._finish(op, 'failed', 'transport_fault')
            elif status.get('worker_state') in ('parked', 'stopping') or not status.get('daemon_running'):
                self._finish(op, 'failed', 'service_suspended')
            elif op['action'] == 'connect' and link and link.get('hid_ready') is True:
                self._finish(op, 'done', 'hid_ready')
            elif op['action'] == 'disconnect' and not link and not any(
                    (item['address'], item['protocol']) == target for item in status.get('device_teardowns', [])):
                self._finish(op, 'done', 'disconnected')
            elif now >= op['deadline']:
                self._finish(op, 'timed_out', 'device_not_ready' if link else 'device_not_available')

    def _make_room(self):
        while len(self.operations) >= self.LIMIT:
            victim = next((key for key, op in self.operations.items() if op['phase'] in TERMINAL), None)
            if victim is None:
                raise ValueError('Operation capacity reached')
            del self.operations[victim]
            self.requests = OrderedDict((key, value) for key, value in self.requests.items() if value != victim)

    def submit(self, action, address, protocol, request_id, status, dispatch):
        """Call on existing event loop; dispatch is a nonblocking intent adapter."""
        target = self._target(address, protocol)
        if action not in ('connect', 'disconnect') or not self._identifier(request_id):
            raise ValueError('Invalid connection request')
        with self.lock:
            self._reconcile(status)
            existing_id = self.requests.get(request_id)
            if existing_id:
                op = self.operations[existing_id]
                if (op['action'], op['address'], op['protocol']) != (action, *target):
                    raise ValueError('Request ID already used for a different intent')
                return deepcopy(op)
            if not self._saved(status, target):
                raise ValueError('Device is not saved')
            if any((item['address'], item['protocol']) == target for item in status.get('device_teardowns', [])):
                raise ValueError('Device disconnect is still active or not confirmed')
            if status.get('worker_fault') or status.get('worker_state') in ('parked', 'stopping') or not status.get('daemon_running'):
                raise ValueError('Bluetooth service unavailable or faulted')
            link_now = self._connection(status, target)
            if (status.get('scanning') or status.get('pairing') or status.get('cursor_running')) and not (
                    action == 'connect' and link_now):
                raise ValueError('Another Bluetooth operation is active')
            for op in self.operations.values():
                if op['phase'] in ACTIVE and (op['address'], op['protocol']) == target:
                    if op['action'] != action:
                        raise ValueError('A conflicting device operation is still active')
                    if len(self.requests) >= self.ALIAS_LIMIT:
                        raise ValueError('Request receipt capacity reached; inspect current operation')
                    self.requests[request_id] = op['id']
                    return deepcopy(op)
            if sum(op['phase'] in ACTIVE for op in self.operations.values()) >= self.ACTIVE_LIMIT:
                raise ValueError('Connection request queue is full')
            self._make_room()
            if len(self.requests) >= self.ALIAS_LIMIT:
                # Terminal receipts can expire; active receipts are never removed.
                self.requests = OrderedDict((key, val) for key, val in self.requests.items()
                                            if self.operations[val]['phase'] in ACTIVE)
            self.sequence += 1
            now = self.clock()
            op = {'id': str(self.sequence), 'request_id': request_id, 'instance_id': self.instance_id,
                  'action': action, 'address': target[0], 'protocol': target[1],
                  'started': now, 'deadline': now + (70 if action == 'connect' else 15),
                  'phase': 'waiting', 'reason': 'queued'}
            self.operations[op['id']] = op
            self.requests[request_id] = op['id']
            link = self._connection(status, target)
            if action == 'connect' and link:
                self.desired[target] = 'connect'
                if link.get('hid_ready') is True:
                    self._finish(op, 'done', 'already_ready')
                else:
                    op['reason'] = 'configuring_hid'
                return deepcopy(op)
            try:
                accepted = dispatch(action, target[0], target[1])
                if accepted is not True:
                    raise ValueError('Host did not accept the connection intent')
                self.desired[target] = action
                op['reason'] = 'connecting' if action == 'connect' else 'disconnecting'
                if action == 'disconnect' and not link:
                    self._finish(op, 'done', 'already_disconnected')
            except Exception as error:
                self._finish(op, 'refused', str(error)[:160])
            return deepcopy(op)

    def cancel(self, operation_id, instance_id, status):
        with self.lock:
            self._reconcile(status)
            if instance_id != self.instance_id or operation_id not in self.operations:
                raise ValueError('Operation belongs to another API instance or expired')
            op = self.operations[operation_id]
            self._finish(op, 'cancelled', 'user_wait_cancelled_auto_policy_unchanged')
            return deepcopy(op)

    def fail(self, action, address, protocol, reason):
        with self.lock:
            for op in self.operations.values():
                if (op['action'], op['address'], op['protocol']) == (action, address, protocol):
                    self._finish(op, 'failed', reason)

    def snapshot(self, status):
        with self.lock:
            self._reconcile(status)
            states = []
            for device in status.get('devices', [])[:64]:
                target = (str(device.get('address', '')).upper(), device.get('protocol'))
                link = self._connection(status, target)
                active = next((op for op in self.operations.values() if op['phase'] in ACTIVE and
                               (op['address'], op['protocol']) == target), None)
                teardown = next((item for item in status.get('device_teardowns', [])
                                 if (item['address'], item['protocol']) == target), None)
                if status.get('worker_fault') or (teardown and teardown.get('state') == 'disconnect_unconfirmed'):
                    observed = 'blocked'
                elif not status.get('daemon_running') or status.get('worker_state') in ('parked', 'stopping'):
                    observed = 'suspended'
                elif active and active['action'] == 'disconnect':
                    observed = 'disconnecting'
                elif link:
                    observed = 'ready' if link.get('hid_ready') is True else 'configuring'
                elif active:
                    observed = 'connecting' if active['action'] == 'connect' else 'disconnecting'
                else:
                    observed = 'disconnected'
                states.append({'address': target[0], 'protocol': target[1], 'state': observed,
                               'desired': self.desired.get(target, 'connect'),
                               'operation_id': active['id'] if active else None})
            return {'schema': self.SCHEMA, 'instance_id': self.instance_id, 'server_time': self.clock(),
                    'operations': deepcopy(list(self.operations.values())), 'device_states': states}
