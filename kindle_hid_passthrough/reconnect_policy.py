"""Single successful HID peer, separate from saved devices and transient intent."""
import json
import os
import threading
import time
from pathlib import Path


class ReconnectPolicy:
    def __init__(self, path, clock=time.monotonic):
        self.path = Path(path)
        self.clock = clock
        self.lock = threading.RLock()
        self.last = None
        self.serial = 0
        self.committed = 0
        self.manual = None
        self.persistence_error = None
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 1024:
                raise ValueError('Invalid reconnect record')
            with open(self.path, encoding='utf-8') as stream:
                text = stream.read(1025)
            if len(text) > 1024:
                raise ValueError('Reconnect record too large')
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError('Invalid reconnect object')
            if data.get('schema') != 1:
                raise ValueError('Unknown reconnect schema')
            self.last = self.key(data['address'], data['protocol'])
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.persistence_error = type(error).__name__

    @staticmethod
    def key(address, protocol):
        address = str(address).split('/')[0].upper()
        protocol = getattr(protocol, 'value', protocol)
        if protocol not in ('ble', 'classic') or len(address) != 17:
            raise ValueError('Invalid reconnect identity')
        if any(len(part) != 2 or any(c not in '0123456789ABCDEF' for c in part)
               for part in address.split(':')) or len(address.split(':')) != 6:
            raise ValueError('Invalid reconnect address')
        return address, protocol

    def _pending(self):
        return self.manual if self.manual and self.manual['until'] > self.clock() else None

    def request(self, address, protocol):
        key = self.key(address, protocol)
        with self.lock:
            self.serial += 1
            self.manual = {'key': key, 'serial': self.serial, 'until': self.clock() + 70.0}
            return self.serial

    def allowed(self, address, protocol):
        key = self.key(address, protocol)
        with self.lock:
            pending = self._pending()
            return key == (pending['key'] if pending else self.last)

    def generation(self, address, protocol):
        key = self.key(address, protocol)
        with self.lock:
            pending = self._pending()
            return pending['serial'] if pending and pending['key'] == key else self.committed

    def ready(self, address, protocol, generation, identity=None):
        key = self.key(address, protocol)
        identity = key if identity is None else self.key(identity, protocol)
        with self.lock:
            pending = self._pending()
            if generation < self.committed:
                return False
            if pending and (pending['key'] != identity or pending['serial'] != generation):
                return False
            if not pending and identity != self.last:
                return False
            previous = self.last
            self.last = key
            self.committed = generation
            if pending:
                self.manual = None
            if key == previous and self.persistence_error is None:
                return True
            temporary = self.path.with_name(self.path.name + '.tmp')
            try:
                if self.path.is_symlink() or temporary.is_symlink():
                    raise OSError('Reconnect record symlink refused')
                data = json.dumps({'schema': 1, 'address': key[0], 'protocol': key[1]}) + '\n'
                with open(temporary, 'w', encoding='utf-8') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                if os.name == 'posix':
                    directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                self.persistence_error = None
            except OSError as error:
                self.persistence_error = type(error).__name__
            return True

    def snapshot(self):
        with self.lock:
            pending = self._pending()
            return {'mode': 'last_hid_ready_only',
                    'last_successful': {'address': self.last[0], 'protocol': self.last[1]} if self.last else None,
                    'manual_target': {'address': pending['key'][0], 'protocol': pending['key'][1]} if pending else None,
                    'persistence_error': self.persistence_error}
