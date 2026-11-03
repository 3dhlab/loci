"""Bounded duplex transfer from an already verified open selection descriptor."""
import hashlib
import os
import selectors
import subprocess
import time


def stream_command(command, source, *, expected_bytes, expected_sha256, timeout=1200):
    if type(expected_bytes) is not int or expected_bytes < 0:
        raise ValueError("Invalid selected transfer size.")
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, bufsize=0)
    deadline = time.monotonic() + timeout
    checksum = hashlib.sha256()
    read = 0
    pending = b""
    output = bytearray()
    try:
        os.set_blocking(child.stdin.fileno(), False)
        os.set_blocking(child.stdout.fileno(), False)
        with selectors.DefaultSelector() as ready:
            ready.register(child.stdin, selectors.EVENT_WRITE)
            ready.register(child.stdout, selectors.EVENT_READ)
            while ready.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Selected transfer timed out.")
                for key, _ in ready.select(min(1, remaining)):
                    if key.fileobj is child.stdin:
                        if not pending:
                            pending = source.read(min(65536, expected_bytes - read + 1))
                            read += len(pending)
                            if read > expected_bytes:
                                raise ValueError("Selected transfer exceeds its sealed size.")
                            checksum.update(pending)
                            if not pending:
                                ready.unregister(child.stdin)
                                child.stdin.close()
                                continue
                        try:
                            count = os.write(child.stdin.fileno(), pending)
                        except BlockingIOError:
                            continue
                        if count <= 0:
                            raise ValueError("Selected transfer made no progress.")
                        pending = pending[count:]
                    else:
                        try:
                            chunk = os.read(child.stdout.fileno(), min(8192, 65537 - len(output)))
                        except BlockingIOError:
                            continue
                        output.extend(chunk)
                        if len(output) > 65536:
                            raise ValueError("Selected transfer output exceeds its allowance.")
                        if not chunk:
                            ready.unregister(child.stdout)
                            child.stdout.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or child.wait(timeout=remaining) != 0:
            raise ValueError("Selected transfer did not complete.")
        if read != expected_bytes or checksum.hexdigest() != expected_sha256:
            raise ValueError("Selected transfer content changed.")
        return bytes(output)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        for stream in (child.stdin, child.stdout):
            if stream is not None and not stream.closed:
                stream.close()
