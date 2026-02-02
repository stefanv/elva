#!/usr/bin/env python3
"""Bridge between Emacs and Elva using pycrdt."""

import asyncio
import json
import sys
from pycrdt import Doc, Text
from websockets import connect


class ElvaBridge:
    def __init__(self, url):
        self.url = url
        self.doc = Doc()
        self.text = self.doc.get("text", type=Text)
        self._applying_remote = False

    def on_text_change(self, event):
        """Called when Yjs text changes (from Elva)."""
        if self._applying_remote:
            return

        # Translate Yjs delta to position-based ops for Emacs
        pos = 0
        for delta in event.delta:
            if "retain" in delta:
                pos += delta["retain"]
            elif "insert" in delta:
                text = delta["insert"]
                self._send_to_emacs({"op": "insert", "pos": pos, "text": text})
                pos += len(text)
            elif "delete" in delta:
                count = delta["delete"]
                self._send_to_emacs({"op": "delete", "pos": pos, "count": count})

    def _send_to_emacs(self, msg):
        """Send JSON message to Emacs via stdout."""
        print(json.dumps(msg), flush=True)

    def apply_from_emacs(self, msg):
        """Apply an edit from Emacs to the Yjs doc."""
        self._applying_remote = True
        try:
            if msg["op"] == "insert":
                self.text.insert(msg["pos"], msg["text"])
            elif msg["op"] == "delete":
                self.text.delete(msg["pos"], msg["count"])
        finally:
            self._applying_remote = False

    async def run(self):
        """Main loop: connect to Elva and bridge to Emacs."""
        self.text.observe(self.on_text_change)

        async with connect(self.url) as ws:
            # Start tasks for both directions
            await asyncio.gather(
                self._read_emacs(ws),
                self._read_elva(ws),
            )

    async def _read_emacs(self, ws):
        """Read from stdin (Emacs) and apply to doc."""
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
        )

        while True:
            line = await reader.readline()
            if not line:
                break
            msg = json.loads(line)
            self.apply_from_emacs(msg)
            # Send update to Elva
            update = self.doc.get_update()
            await ws.send(update)

    async def _read_elva(self, ws):
        """Read from Elva WebSocket and apply to doc."""
        async for message in ws:
            self._applying_remote = True
            try:
                self.doc.apply_update(message)
            finally:
                self._applying_remote = False


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:8000"
    bridge = ElvaBridge(url)
    asyncio.run(bridge.run())
