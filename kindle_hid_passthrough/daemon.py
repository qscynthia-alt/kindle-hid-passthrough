#!/usr/bin/env python3
"""Persistent connection manager with auto-reconnect + embedded API server."""

import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading

sys.path.insert(0, '/mnt/us/kindle_hid_passthrough')

import button_mapper
from api_server import PORT, APIServer, RequestHandler
from bt_setup import chip, prepare_bt
from config import config, get_version
from controller import DaemonController
from host import HIDHost
from logging_utils import errstr, log, setup_daemon_logging
from power_monitor import PowerMonitor
from scanner import Scanner
from reconnect_policy import ReconnectPolicy

logger = logging.getLogger(__name__)

SUSPEND_TIMEOUT = 10.0


class HIDDaemon:
    """Daemon that maintains persistent connection to an HID device."""

    def __init__(self):
        self.running = False
        self.connection_policy = {}
        self.reconnect_policy = ReconnectPolicy(os.path.join(config.base_path, 'last-hid-device.json'))
        self.host = None
        self._host_task = None
        self._suspended = False
        self.transport_released = True
        self.worker_fault = None
        self._fault_stop = asyncio.Event()
        self._resume_event = asyncio.Event()
        self._paired_host = None
        self._release_task = None
        self._release_fault = None
        self._resume_after_release = False
        self._operation_owner = None
        # Set in main(): called with True/False as a pointer device connects/leaves.
        self.on_pointer_change = None

    @property
    def connection_state(self) -> dict:
        """Current connection state for API."""
        if self.host and not self._suspended:
            return self.host.connection_state
        return {"connected": False, "connections": []}

    async def suspend(self):
        """Disconnect and release transport for scan/pair."""
        if self.worker_fault and self.worker_fault != self._release_fault:
            raise RuntimeError(self.worker_fault)
        logger.info("Daemon suspending...")
        self._suspended = True
        self._resume_after_release = False
        self._resume_event.clear()
        task = self._begin_release()
        await self._wait_release(task)

    async def _wait_release(self, task):
        if task is None:
            return
        await self._abandon_after_timeout(task, 'controller drain')
        if task.done():
            self._release_completed(task)
            if task.cancelled() or task.exception() is not None:
                raise RuntimeError(self.worker_fault)
            return
        self._release_fault = 'Controller drain pending; waiting for physical completion'
        self.worker_fault = self._release_fault
        raise RuntimeError(self.worker_fault)

    def _begin_release(self):
        """One retained release transaction; repeated requests never replay it."""
        if self._release_task is not None and not self._release_task.done():
            return self._release_task
        if self._release_task is not None and (self._release_task.cancelled() or self._release_task.exception() is not None):
            return self._release_task
        owner = self._operation_owner or self.host
        if owner is None:
            self.transport_released = True
            return None
        host, worker = owner, self._host_task
        self._release_task = asyncio.create_task(self._release_host(host, worker))
        self._release_task.add_done_callback(self._release_completed)
        return self._release_task

    async def _release_host(self, host, worker):
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.wait({worker})
        if worker is not None and not worker.cancelled():
            worker.exception()
        handlers = [t for t in getattr(host, '_handler_tasks', ()) if not t.done()]
        if handlers:
            await asyncio.wait(handlers)
        owners = [o.task for o in getattr(host, '_radio_attempts', ())]
        if owners:
            await asyncio.wait(owners)
            # Host.cleanup still refuses an owner whose task failed without
            # establishing hardware completion; never infer idle from done().
        setup = [t for t in getattr(host, '_connection_tasks', ()) if not t.done()]
        for task in setup:
            task.cancel()
        if setup:
            await asyncio.wait(setup)
        commands = list(getattr(getattr(getattr(host, 'device', None), 'host', None), '_command_transactions', ()))
        if commands:
            await asyncio.wait(commands)
        await host.cleanup()
        if self.host is host:
            self.host = None
            self._host_task = None
        if self._operation_owner is host:
            self._operation_owner = None
        self.transport_released = True

    def _release_completed(self, task):
        if task is not self._release_task:
            return
        if task.cancelled():
            self.worker_fault = 'Controller drain interrupted; ownership unconfirmed'
            return
        error = task.exception()
        if error is not None:
            self.worker_fault = 'Transport release unverified: ' + str(error)
            return
        if self.worker_fault == self._release_fault:
            self.worker_fault = None
        self._release_fault = None
        if self._resume_after_release and self.running and not self.worker_fault:
            self._suspended = False
            self._resume_event.set()

    @staticmethod
    async def _abandon_after_timeout(task, what):
        """Wait out a teardown task, then leave it behind if it will not end.

        asyncio.wait() is deliberate: wait_for() re-cancels and awaits, which
        hangs forever on a task that ignores cancellation.
        """
        done, _ = await asyncio.wait({task}, timeout=SUSPEND_TIMEOUT)
        if not done:
            logger.warning(f"Suspend: {what} did not finish, abandoning it")
            return False
        try:
            task.result()
        except asyncio.CancelledError:
            return True
        except Exception:
            return False
        return True

    async def scan(self, duration=10.0, on_device_found=None):
        """Scan for BT devices. Must be called while suspended."""
        scanner = Scanner()
        if self._operation_owner is not None:
            raise RuntimeError('Previous scan/pair still owns transport')
        self._operation_owner = scanner
        self.transport_released = False
        if on_device_found:
            scanner.on_device_found = on_device_found
        try:
            await scanner.start()
            await scanner.scan(duration=duration)
        finally:
            await self._wait_release(self._begin_release())

    async def pair(self, address, protocol, name=None) -> bool:
        """Pair with a device. Must be called while suspended."""
        host = HIDHost()
        host.reconnect_policy = self.reconnect_policy
        self.reconnect_policy.request(address, protocol)
        if self._operation_owner is not None:
            raise RuntimeError('Previous scan/pair still owns transport')
        self._operation_owner = host
        self.transport_released = False
        success = False
        try:
            success = await host.pair_device(address, protocol, name)
            if success:
                session = host._pairing_session
                session.reconnect_generation = self.reconnect_policy.request(session.address, session.protocol)
                self._paired_host = host
                self._operation_owner = None
                return True
            return False
        finally:
            if not success:
                await self._wait_release(self._begin_release())

    async def disconnect(self, address=None):
        """Drop one session by address, or all; daemon keeps running and
        the connect loops will reconnect."""
        if self.host and not self._suspended:
            await self.host.end_session(address)
        else:
            logger.info("No active connection to disconnect")

    async def resume(self):
        """Resume connections after scan/pair."""
        self._resume_after_release = True
        if self._release_task is not None and not self._release_task.done():
            return False
        if self._release_task is not None:
            self._release_completed(self._release_task)
        if self.worker_fault:
            raise RuntimeError(self.worker_fault)
        logger.info("Daemon resuming...")
        self._suspended = False
        self._resume_event.set()
        return True

    def _has_devices(self, log_details=False) -> bool:
        """Check if any devices are configured."""
        devices = config.get_all_devices()
        if not devices:
            return False

        if log_details:
            if len(devices) == 1 and devices[0][0] != '*':
                addr, proto, name = devices[0]
                display = f"{name} ({addr})" if name else addr
                logger.info(f"Device: {display} ({proto.value})")
            else:
                logger.info(f"Accepting {len(devices)} device(s):")
                for addr, proto, name in devices:
                    display = f"{name} ({addr})" if name else addr
                    logger.info(f"  - {display} ({proto.value})")

        return True

    async def run(self):
        """Main daemon loop."""
        self.running = True

        logger.info(f"HID Daemon v{get_version()}")

        while self.running:
            if self.worker_fault:
                release = self._release_task
                if release is not None and release.done() and self.worker_fault == self._release_fault:
                    self._release_completed(release)
                    if not self.worker_fault:
                        continue
                if release is None or release.done() or self.worker_fault != self._release_fault:
                    await self._fault_stop.wait()
                    break
                stop = asyncio.create_task(self._fault_stop.wait())
                try:
                    await asyncio.wait({release, stop}, return_when=asyncio.FIRST_COMPLETED)
                    if self._fault_stop.is_set():
                        break
                finally:
                    stop.cancel()
                    await asyncio.gather(stop, return_exceptions=True)
                continue
            # Wait for devices if none configured or after suspend
            if self._suspended:
                logger.info("Daemon suspended, waiting for resume...")
                await self._resume_event.wait()
                self._resume_event.clear()
                if not self.running:
                    break

            if not self._has_devices(log_details=True) \
                    and not config.media_remote_enabled:
                logger.info("No devices configured, waiting for pairing...")
                self._resume_event.clear()
                await self._resume_event.wait()
                self._resume_event.clear()  # else the next reconnect delay is skipped
                if not self.running:
                    break
                continue

            if self.worker_fault or not self.running:
                continue
            chip().ensure_powered()

            try:
                # Use handed-off host from controller pairing if available
                if self._paired_host:
                    logger.info("=== Continuing with paired device ===")
                    self.transport_released = False
                    self.host = self._paired_host
                    self._paired_host = None
                    self.host.connection_policy = self.connection_policy
                    self.host.reconnect_policy = self.reconnect_policy
                    self.host.on_pointer_change = self.on_pointer_change
                    self._host_task = asyncio.create_task(
                        self.host.continue_after_pairing()
                    )
                else:
                    logger.info("=== Starting connection ===")
                    self.transport_released = False
                    self.host = HIDHost()
                    self.host.connection_policy = self.connection_policy
                    self.host.reconnect_policy = self.reconnect_policy
                    self.host.on_pointer_change = self.on_pointer_change
                    self._host_task = asyncio.create_task(
                        self.host.run()
                    )
                await self._host_task

            except asyncio.CancelledError:
                if self._suspended:
                    logger.info("Connection cancelled (suspend)")
                elif not self.running:
                    logger.info("Cancelled (shutdown)")
                    break
                else:
                    logger.info("Connection cancelled, will reconnect")

            except Exception as e:
                logger.error(f"Error: {errstr(e)}")

            finally:
                self._host_task = None
                # When suspended, suspend() owns cleanup of self.host. Skipping
                # here avoids a race where both paths run host.cleanup() in
                # parallel and deadlock on transport/connection teardown.
                if self.host and not self._suspended and not self.worker_fault:
                    try:
                        self._resume_after_release = True
                        task = self._begin_release()
                        await self._wait_release(task)
                    except Exception as exc:
                        if self.worker_fault != self._release_fault:
                            self.worker_fault = "Transport cleanup unverified: " + str(exc)
                        self._suspended = True

            if not self.running:
                break

            # Don't delay if we got suspended during connection
            if self._suspended:
                continue

            logger.info(f"Reconnecting in {config.reconnect_delay}s...")
            try:
                await asyncio.wait_for(
                    self._resume_event.wait(),
                    timeout=config.reconnect_delay
                )
                # Resume event fired during delay, go back to top
                self._resume_event.clear()
            except asyncio.TimeoutError:
                pass  # Normal delay elapsed

        logger.info("Daemon stopped")

    async def stop(self):
        """Stop the daemon."""
        logger.info("Stopping...")
        self.running = False
        self._resume_after_release = False
        self.connection_policy = {}
        self._fault_stop.set()
        # Wake up if suspended (waiting on _resume_event)
        self._resume_event.set()
        if self.host and not self.worker_fault:
            try:
                await self.host.cleanup()
            except Exception:
                pass


async def main():
    setup_daemon_logging(config.log_file)

    # Devices paired before the mapper was installed (or before this existed)
    # get their block on the next daemon start.
    button_mapper.register_all(config.get_all_devices())

    prepare_bt()

    daemon = HIDDaemon()
    controller = DaemonController(daemon)
    controller.loop = asyncio.get_event_loop()

    # Reap any cursor overlay orphaned by a previous daemon before we start.
    subprocess.run(['killall', '-q', 'mousecursor'], capture_output=True)

    # Auto-toggle the mouse cursor overlay as pointer devices connect/disconnect.
    def _cursor_for_pointer(active):
        if active:
            controller.request_cursor_start()
        else:
            controller.request_cursor_stop()
    daemon.on_pointer_change = _cursor_for_pointer

    # Start embedded API server
    server = APIServer(('127.0.0.1', PORT), RequestHandler)
    server.controller = controller
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    log.info(f"API server listening on port {PORT}")

    monitor = PowerMonitor(controller)
    monitor.start()
    log.info("Watching powerd for system suspend")

    # Signal handling
    shutdown = asyncio.Event()

    def on_signal():
        logger.info("Shutdown signal received")
        shutdown.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, on_signal)

    log.info(f"Kindle HID Passthrough v{get_version()} (daemon)")
    daemon_task = asyncio.create_task(daemon.run())

    await asyncio.wait(
        [daemon_task, asyncio.create_task(shutdown.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )

    if shutdown.is_set():
        await daemon.stop()
        if not daemon_task.done():
            daemon_task.cancel()
            try:
                await daemon_task
            except asyncio.CancelledError:
                pass

    monitor.stop()
    server.shutdown()
    logger.info("Daemon stopped")


if __name__ == '__main__':
    asyncio.run(main())
