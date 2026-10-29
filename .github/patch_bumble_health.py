from pathlib import Path
import bumble.host
p=Path(bumble.host.__file__)
s=p.read_text();start=s.index('    def on_command_processed(');end=s.index('    ############################################################',start)
part=s[start:end];assert part.count('if self.pending_response:')==1
part=part.replace('if self.pending_response:','if self.pending_response is not None and not self.pending_response.done():',1)
p.write_text(s[:start]+part+s[end:])
