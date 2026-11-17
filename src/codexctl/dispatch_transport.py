"""Bridge the user's shared App Server WebSocket to newline-delimited JSON."""
from __future__ import annotations

import sys
import threading

from websockets.sync.client import unix_connect


def main() -> None:
    with unix_connect(sys.argv[1], uri="ws://localhost", compression=None, max_size=16 * 1024 * 1024) as connection:
        def forward_input():
            try:
                for line in sys.stdin:
                    connection.send(line.rstrip("\n"))
            finally:
                connection.close()

        threading.Thread(target=forward_input, daemon=True).start()
        for message in connection:
            print(message, flush=True)


if __name__ == "__main__":
    main()
