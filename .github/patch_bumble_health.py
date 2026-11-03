from pathlib import Path
import bumble.host
p=Path(bumble.host.__file__)
s=p.read_text();start=s.index('    def on_command_processed(');end=s.index('    ############################################################',start)
part=s[start:end];assert part.count('if self.pending_response:')==1
part=part.replace('if self.pending_response:','if self.pending_response is not None and not self.pending_response.done():',1)
needle = '        if self.pending_response is not None and not self.pending_response.done():'
assert part.count(needle) == 1
part = part.replace(needle, """        # Also protect the credit path when the current future is already done.
        if self.pending_command is not None and self.pending_command.op_code != event.command_opcode:
            logger.warning('Ignoring unmatched command response')
            return
        if self.pending_command is None and self.pending_response is not None and not self.pending_response.done():
            logger.warning('Ignoring response without command identity')
            return
""" + needle, 1)
p.write_text(s[:start]+part+s[end:])

# Keep physical command transactions alive when a Device timeout or caller
# cancellation stops waiting. A started command owns its response slot until
# its actual ACK, including commands unrelated to connection initiation.
s=p.read_text()
start=s.index('    async def _send_command(')
body=s.index('        # Wait until we can send',start)
end=s.index('    @overload',body)
s=s[:body]+'''        transactions = getattr(self, '_command_transactions', None)
        if transactions is None:
            transactions = self._command_transactions = set()
        if len(transactions) >= 64:
            error = RuntimeError('HCI command queue capacity reached')
            error.command_not_sent = True
            raise error
        started = False

        async def transaction():
            nonlocal started
            await self.command_semaphore.acquire()
            started = True
            response = None
            try:
                if self.pending_command is not None or self.pending_response is not None:
                    raise RuntimeError('Previous HCI command still owns response slot')
                self.pending_response = asyncio.get_running_loop().create_future()
                self.pending_command = command
                self.send_hci_packet(command)
                response = await self.pending_response
                return response
            except BaseException:
                self._command_fault = 'HCI command ended without a confirmed response'
                raise
            finally:
                self.pending_command = None
                self.pending_response = None
                if response is not None and response.num_hci_command_packets and self.command_semaphore.locked():
                    self.command_semaphore.release()

        task = asyncio.create_task(transaction())
        transactions.add(task)
        def completed(done):
            transactions.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(completed)
        try:
            return await asyncio.wait_for(asyncio.shield(task), response_timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            if not started:
                # A queued request has made no hardware change. Do not replay
                # it after its owner has already abandoned the operation.
                task.cancel()
            raise

'''+s[end:]
p.write_text(s)
