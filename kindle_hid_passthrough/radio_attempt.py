"""Event-driven ownership of an HCI initiator, including cancellation drain.

The caller may stop waiting; the physical owner must outlive that caller.
Only a terminal controller event AND all command responses allow lock release.
"""
import asyncio


class RadioDrainPending(RuntimeError):
    """Receipt deadline ended; the retained hardware owner is still draining."""


class RadioAttempt:
    def __init__(self, host, pending, create, cancel, detach, abandon=None,
                 rejection_errors=()):
        self.host, self.pending = host, pending
        self.create, self.cancel = create, cancel
        self.detach, self.abandon = detach, abandon
        self.stop = asyncio.Event()
        self.state = 'creating'
        self.abandoned = False
        self.adopted = False
        self.rejection_errors = rejection_errors
        self.task = None
        pending.add_done_callback(lambda future: None if future.cancelled() else future.exception())

    async def wait(self, window, retry=None, caller_timeout=None):
        owners = getattr(self.host, '_radio_attempts', None)
        if owners is None:
            owners = self.host._radio_attempts = set()
        owners.add(self)
        self.task = asyncio.create_task(self._run(window, retry))
        self.task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        try:
            if caller_timeout is not None:
                done, _ = await asyncio.wait({self.task}, timeout=caller_timeout)
                if not done:
                    self.abandoned = True
                    self.stop.set()
                    raise RadioDrainPending('Controller result pending; retained operation will finish on its physical response')
                return self.task.result()
            return await asyncio.shield(self.task)
        except asyncio.CancelledError:
            self.abandoned = True
            self.stop.set()
            # Normal suspend/stop gives the event-driven drain a bounded grace
            # period. Unknown hardware remains owned and blocks host cleanup.
            await asyncio.wait({self.task}, timeout=getattr(self.host, '_radio_cancel_grace', 2.0))
            if self.task.done() and not self.task.cancelled() and self.task.exception() is None:
                self._adopt_abandoned(self.task.result())
            raise

    def _adopt_abandoned(self, result):
        if result is not None and self.abandoned and not self.adopted and self.abandon is not None:
            self.adopted = True
            self.abandon(result)

    async def _run(self, window, retry):
        stop_task = asyncio.create_task(self.stop.wait())
        retry_task = asyncio.create_task(retry.wait()) if retry is not None else None
        command = asyncio.create_task(self.create())
        cancel_command = None
        retired = False
        try:
            # Never cancel send_command: a late response may still own its slot.
            await asyncio.shield(command)
            self.state = 'connecting'
            waits = {self.pending, stop_task}
            if retry_task is not None:
                waits.add(retry_task)
            await asyncio.wait(waits, timeout=window,
                               return_when=asyncio.FIRST_COMPLETED)
            if not self.pending.done():
                self.state = 'cancelling'
                cancel_command = asyncio.create_task(self.cancel())
                # Do not let an ACK failure discard the terminal-event watcher.
                await asyncio.wait({cancel_command, self.pending}, timeout=2.0,
                                   return_when=asyncio.ALL_COMPLETED)
                if not cancel_command.done() or not self.pending.done():
                    self.state = 'awaiting_controller'
                await asyncio.shield(cancel_command)
            self.state = 'draining'
            # A failed cancel command is handled below without yielding ownership.
            await asyncio.wait({self.pending})
            retired = True
            if self.pending.cancelled():
                retired = False
                raise RuntimeError('Physical result watcher was cancelled')
            error = self.pending.exception()
            result = None if error else self.pending.result()
            self._adopt_abandoned(result)
            return result
        except Exception as error:
            if retired:
                raise
            if cancel_command is None and (isinstance(error, self.rejection_errors) or getattr(error, 'command_not_sent', False)):
                # A negative Create Command Status explicitly confirms that no
                # procedure was started; no terminal connection event follows.
                retired = True
                if self.pending.done() and not self.pending.cancelled():
                    self.pending.exception()
                return None
            failed_command = cancel_command if cancel_command is not None else command
            command_error = failed_command.exception() if failed_command.done() and not failed_command.cancelled() else None
            if not isinstance(command_error, self.rejection_errors):
                self.state = 'transport_unconfirmed'
                # A transport exception has no ACK. No subsequent owner may use
                # the command slot, even if a connection event arrives later.
                await asyncio.Future()
            # Errors are not physical-idle evidence. Retain this owner until the
            # matching terminal event arrives, even when command submission fails.
            self.state = 'awaiting_controller'
            await asyncio.wait({self.pending})
            if self.pending.cancelled():
                raise RuntimeError('Physical result watcher was cancelled')
            retired = True
            error = self.pending.exception()
            result = None if error else self.pending.result()
            self._adopt_abandoned(result)
            return result
        finally:
            for task in (stop_task, retry_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(t for t in (stop_task, retry_task) if t is not None),
                                 return_exceptions=True)
            if retired:
                self.state = 'retired'
                self.detach()
                self.host._radio_attempts.discard(self)
                self.host._radio_lock.release()
            else:
                self.state = 'controller_unconfirmed'


class RadioScan(RadioAttempt):
    """A scan holds the radio through the successful Disable Scan response."""

    async def _run(self, window, retry):
        stop_task = asyncio.create_task(self.stop.wait())
        retry_task = asyncio.create_task(retry.wait()) if retry is not None else None
        start = asyncio.create_task(self.create())
        retired = False
        try:
            try:
                await asyncio.shield(start)
            except Exception:
                # Start comprises multiple HCI commands. An exception cannot
                # tell whether scan enable reached the controller. Disable it.
                self.state = 'start_unconfirmed'
            else:
                self.state = 'scanning'
                waits = {self.pending, stop_task}
                if retry_task is not None:
                    waits.add(retry_task)
                await asyncio.wait(waits, timeout=window,
                                   return_when=asyncio.FIRST_COMPLETED)
            self.state = 'stopping_scan'
            stop_command = asyncio.create_task(self.cancel())
            await asyncio.shield(stop_command)
            retired = True
            return self.pending.result() if self.pending.done() and not self.pending.cancelled() else None
        except Exception:
            self.state = 'scan_stop_unconfirmed'
            await asyncio.Future()
        finally:
            stop_task.cancel()
            if retry_task is not None:
                retry_task.cancel()
            await asyncio.gather(*(t for t in (stop_task, retry_task) if t is not None), return_exceptions=True)
            if retired:
                self.state = 'retired'
                self.detach()
                self.host._radio_attempts.discard(self)
                self.host._radio_lock.release()
            else:
                self.state = 'controller_unconfirmed'

