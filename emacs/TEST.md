# Testing the Emacs Client

## Start the Server

```bash
# Uses default port 7654
.venv/bin/elva server

# Or specify a custom port
.venv/bin/elva server --port 46795
```

## In Emacs

```elisp
;; Load the package (adjust path as needed)
(load-file "~/src/elva/emacs/elva.el")

;; Set Python to use the venv (required for pycrdt/websockets)
(setq elva-bridge-program "~/src/elva/.venv/bin/python3")

;; Connect a buffer (default host:port is localhost:7654)
;; Room IDs must be 10-250 characters
M-x elva-connect RET my-room-0001 RET

;; Or with explicit host/port:
M-x elva-connect RET localhost:7654/my-room-0001 RET
```

## Test with Elva Editor (second client)

```bash
# Uses default port 7654
.venv/bin/elva editor --host localhost --identifier my-room-0001

# Or specify a custom port
.venv/bin/elva editor --host localhost --port 46795 --identifier my-room-0001
```

## Useful Commands

- `M-x elva-status` - check connection
- `M-x elva-disconnect` - disconnect

## Server API

### List Rooms

Using the CLI:
```bash
# Uses default port 7654
.venv/bin/elva rooms

# Or specify host/port
.venv/bin/elva rooms --host localhost --port 7654
```

Or via HTTP:
```bash
curl http://localhost:7654/rooms
```

Returns JSON with active rooms:
```json
{
  "rooms": [
    {"identifier": "my-room-0001", "clients": 2, "persistent": true}
  ],
  "count": 1
}
```

## Testing Scenarios

### Basic sync
1. Start server
2. Connect Emacs buffer
3. Connect Elva editor with same room ID
4. Type in one, verify it appears in the other

### Multi-byte characters
1. Type emoji or CJK characters in Emacs
2. Verify they appear correctly in Elva editor
3. Type in Elva editor, verify in Emacs

### Reconnection
1. Connect Emacs to server
2. Kill the server
3. Observe reconnection attempts in Emacs
4. Restart server
5. Verify reconnection succeeds
