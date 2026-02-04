;;; elva.el --- Collaborative editing via Elva/Yjs -*- lexical-binding: t -*-

;;; Commentary:
;;
;; This package provides collaborative editing for Emacs via Elva,
;; a Yjs-based collaboration server.  It uses a Python subprocess
;; (elva-bridge.py) that handles Yjs protocol communication.
;;
;; Usage:
;;   M-x elva-connect RET my-room-0001 RET
;;
;; URL formats accepted (room IDs must be 10-250 chars):
;;   my-room-0001                 -> ws://localhost:7654/my-room-0001
;;   myhost/my-room-0001          -> ws://myhost:7654/my-room-0001
;;   myhost:8000/my-room-0001     -> ws://myhost:8000/my-room-0001
;;   ws://myhost:8000/my-project   -> ws://myhost:8000/my-project
;;
;; Requirements:
;;   - Python 3.8+
;;   - pycrdt: pip install pycrdt
;;   - websockets: pip install websockets

;;; Code:

(require 'json)
(require 'cl-lib)

(defgroup elva nil
  "Collaborative editing via Elva/Yjs."
  :group 'tools
  :prefix "elva-")

(defcustom elva-bridge-program "python3"
  "Program to run the bridge script."
  :type 'string
  :group 'elva)

(defcustom elva-bridge-script
  (expand-file-name "elva-bridge.py"
                    (file-name-directory (or load-file-name buffer-file-name)))
  "Path to the elva-bridge.py script."
  :type 'string
  :group 'elva)

(defvar-local elva--process nil
  "The bridge subprocess for this buffer.")

(defvar-local elva--applying-remote nil
  "Non-nil when applying remote changes (to prevent echo).")

(defvar-local elva--pending-changes nil
  "List of pending changes to send, in reverse order.")

(defvar-local elva--batch-timer nil
  "Timer for batching rapid changes.")

(defcustom elva-batch-delay 0.05
  "Delay in seconds before sending batched changes.
Set to 0 to disable batching and send changes immediately."
  :type 'number
  :group 'elva)

(defcustom elva-reconnect-delay 2.0
  "Delay in seconds before attempting to reconnect."
  :type 'number
  :group 'elva)

(defcustom elva-max-reconnect-attempts 5
  "Maximum number of reconnection attempts.
Set to 0 to disable automatic reconnection."
  :type 'integer
  :group 'elva)

(defcustom elva-default-port 7654
  "Default port for Elva server connections."
  :type 'integer
  :group 'elva)

(defcustom elva-default-host "localhost"
  "Default host for Elva server connections."
  :type 'string
  :group 'elva)

(defvar-local elva--url nil
  "The URL this buffer is connected to.")

(defvar-local elva--room-id nil
  "The room ID this buffer is connected to.")

(defvar-local elva--push-buffer-on-sync nil
  "If non-nil, push buffer content to room after sync completes.
Used by `elva-room-from-buffer' to send buffer only if room is empty.
Values: nil, `if-empty' (push only if room empty), `always' (reconnect).")

(defvar-local elva--saved-content nil
  "Saved buffer content for restoration during reconnection.")

(defun elva--validate-room-id (room-id)
  "Validate ROOM-ID and return an error message or nil if valid.
Room IDs must be 10-250 characters, containing only letters, numbers,
hyphens, and underscores."
  (cond
   ((< (length room-id) 10)
    (format "Room ID too short (%d chars, need 10-250)" (length room-id)))
   ((> (length room-id) 250)
    (format "Room ID too long (%d chars, max 250)" (length room-id)))
   ((not (string-match "^[A-Za-z0-9_-]+$" room-id))
    "Room ID must contain only letters, numbers, hyphens, underscores")
   (t nil)))

(defun elva--normalize-url (input)
  "Normalize INPUT into a full WebSocket URL.
Accepts various formats (room IDs must be 10-250 chars):
  my-room-0001              -> ws://localhost:7654/my-room-0001
  host/my-room-0001         -> ws://host:7654/my-room-0001
  host:port/my-room-0001    -> ws://host:port/my-room-0001
  ws://host:port/my-project  -> ws://host:port/my-project"
  (let ((url input))
    ;; Strip ws:// or wss:// prefix if present
    (when (string-match "^wss?://" url)
      (setq url (replace-match "" nil nil url)))
    ;; Now parse what's left: [host[:port]]/room-id or just room-id
    (cond
     ;; Has a slash - could be host/room or host:port/room
     ((string-match "^\\([^/:]+\\)\\(:[0-9]+\\)?/\\(.+\\)$" url)
      (let ((host (match-string 1 url))
            (port (match-string 2 url))
            (room (match-string 3 url)))
        ;; Validate room ID
        (when-let ((err (elva--validate-room-id room)))
          (error "Invalid room ID: %s" err))
        (format "ws://%s%s/%s"
                host
                (or port (format ":%d" elva-default-port))
                room)))
     ;; No slash - assume it's just a room ID
     ((not (string-match "/" url))
      ;; Validate room ID
      (when-let ((err (elva--validate-room-id url)))
        (error "Invalid room ID: %s" err))
      (format "ws://%s:%d/%s" elva-default-host elva-default-port url))
     ;; Fallback - return as-is with ws:// prefix
     (t
      (concat "ws://" url)))))

(defvar-local elva--reconnect-count 0
  "Number of reconnection attempts made.")

(defvar-local elva--reconnect-timer nil
  "Timer for reconnection attempts.")

(defun elva--extract-room-id (url)
  "Extract the room ID from a WebSocket URL."
  (if (string-match "/\\([^/]+\\)$" url)
      (match-string 1 url)
    url))

(defun elva--modeline-string ()
  "Return a string for the modeline showing Elva connection status."
  (cond
   ((and elva--room-id elva--process (process-live-p elva--process))
    (propertize (format " Elva[%s] " elva--room-id)
                'face 'success))
   (elva--room-id
    (propertize (format " Elva[%s|OFFLINE] " elva--room-id)
                'face 'warning))
   (elva--reconnect-timer
    (propertize " Elva[reconnecting...] "
                'face 'warning))))

;; Add to modeline
(add-to-list 'mode-line-misc-info
             '(:eval (elva--modeline-string)))

(defun elva-connect (url)
  "Connect to Elva room and open it in a new buffer.
Creates a new buffer named after the room ID and displays the room contents.
URL can be in various formats (room IDs must be 10-250 chars):
  my-room-0001              -> ws://localhost:7654/my-room-0001
  host/my-room-0001         -> ws://host:7654/my-room-0001
  host:port/my-room-0001    -> ws://host:port/my-room-0001
  ws://host:port/my-project  -> ws://host:port/my-project"
  (interactive
   (list (read-string "Elva room (10+ chars, e.g. my-room-0001): ")))
  (let* ((full-url (elva--normalize-url url))
         (room-id (elva--extract-room-id full-url))
         (buf-name (format "*elva:%s*" room-id))
         (buffer (get-buffer buf-name)))
    ;; Check if already connected to this room
    (when (and buffer
               (buffer-live-p buffer)
               (with-current-buffer buffer elva--process)
               (process-live-p (with-current-buffer buffer elva--process)))
      (pop-to-buffer buffer)
      (error "Already connected to room '%s'" room-id))
    ;; Create or reuse buffer for the room
    (setq buffer (get-buffer-create buf-name))
    (pop-to-buffer buffer)
    (with-current-buffer buffer
      ;; Clear any existing content to avoid duplication
      (erase-buffer)
      (setq elva--url full-url)
      (setq elva--room-id room-id)
      (setq elva--reconnect-count 0)
      (elva--do-connect full-url nil))))

(defun elva-room-from-buffer (url)
  "Push current buffer contents to an Elva room.
Fails if the room already has content.
URL can be in various formats (room IDs must be 10-250 chars)."
  (interactive
   (list (read-string "Elva room to push buffer to: ")))
  (when elva--process
    (error "Already connected.  Use `elva-disconnect' first"))
  (let* ((full-url (elva--normalize-url url))
         (room-id (elva--extract-room-id full-url)))
    (setq elva--url full-url)
    (setq elva--room-id room-id)
    (setq elva--reconnect-count 0)
    (setq elva--push-buffer-on-sync 'if-empty)  ; Will push after sync if room is empty
    (elva--do-connect full-url nil)
    (message "Elva: connecting, will push buffer if room is empty...")))

(defun elva--do-connect (url &optional send-buffer-content)
  "Internal function to establish connection to URL.
If SEND-BUFFER-CONTENT is non-nil, send current buffer content to room."
  (let ((buffer (current-buffer)))
    (setq elva--process
          (make-process
           :name "elva-bridge"
           :buffer (generate-new-buffer " *elva-bridge*")
           :command (list elva-bridge-program elva-bridge-script url)
           :connection-type 'pipe
           :stderr (generate-new-buffer " *elva-bridge-stderr*")
           :filter (lambda (proc output)
                     (elva--filter proc output buffer))
           :sentinel (lambda (proc event)
                       (elva--sentinel proc event buffer)))))
  ;; Send initial buffer content only if requested
  (when (and send-buffer-content (zerop elva--reconnect-count))
    (let ((content (buffer-string)))
      (when (> (length content) 0)
        (elva--send `((op . "insert") (pos . 0) (text . ,content))))))
  ;; Watch for local changes
  (add-hook 'after-change-functions #'elva--after-change nil t)
  (message "Connected to Elva: %s" url)
  (force-mode-line-update))

(defun elva--after-change (beg end old-len)
  "Handle local buffer change between BEG and END.
OLD-LEN is the length of the replaced text."
  (unless elva--applying-remote
    ;; Handle deletion
    (when (> old-len 0)
      (elva--queue-change `((op . "delete")
                            (pos . ,(1- beg))      ; Convert to 0-indexed
                            (count . ,old-len))))
    ;; Handle insertion
    (when (> end beg)
      (let ((text (buffer-substring-no-properties beg end)))
        (elva--queue-change `((op . "insert")
                              (pos . ,(1- beg))    ; Convert to 0-indexed
                              (text . ,text)))))))

(defun elva--queue-change (change)
  "Queue CHANGE to be sent, batching rapid changes together."
  (push change elva--pending-changes)
  (if (zerop elva-batch-delay)
      ;; No batching - send immediately
      (elva--flush-changes)
    ;; Cancel existing timer and start a new one
    (when elva--batch-timer
      (cancel-timer elva--batch-timer))
    (setq elva--batch-timer
          (run-at-time elva-batch-delay nil
                       #'elva--flush-changes-in-buffer
                       (current-buffer)))))

(defun elva--flush-changes-in-buffer (buffer)
  "Flush pending changes in BUFFER."
  (when (buffer-live-p buffer)
    (with-current-buffer buffer
      (elva--flush-changes))))

(defun elva--flush-changes ()
  "Send all pending changes to the bridge."
  (setq elva--batch-timer nil)
  (when elva--pending-changes
    ;; Send changes in order (they were pushed in reverse)
    (dolist (change (nreverse elva--pending-changes))
      (elva--send change))
    (setq elva--pending-changes nil)))

(defun elva--send (msg)
  "Send MSG as JSON to the bridge process."
  (when (and elva--process (process-live-p elva--process))
    (process-send-string elva--process
                         (concat (json-encode msg) "\n"))))

(defun elva--filter (proc output buffer)
  "Handle OUTPUT from bridge PROC, applying changes to BUFFER."
  (when (buffer-live-p buffer)
    (dolist (line (split-string output "\n" t))
      (condition-case err
          (when (> (length line) 0)
            (let ((msg (json-parse-string line :object-type 'alist)))
              (elva--apply-remote msg buffer)))
        (error (message "Elva: error parsing message: %s" err))))))

(defun elva--apply-remote (msg buffer)
  "Apply remote change MSG to BUFFER.
Adjusts point appropriately when edits occur before the cursor."
  (with-current-buffer buffer
    (let ((elva--applying-remote t)
          (inhibit-modification-hooks t)
          (old-point (point)))
      (pcase (alist-get 'op msg)
        ("insert"
         (let ((pos (1+ (alist-get 'pos msg)))  ; Convert to 1-indexed
               (text (alist-get 'text msg)))
           (save-excursion
             (goto-char pos)
             (insert text))
           ;; Adjust point if insert was before cursor
           (when (< pos old-point)
             (goto-char (+ old-point (length text))))))
        ("delete"
         (let ((pos (1+ (alist-get 'pos msg)))  ; Convert to 1-indexed
               (count (alist-get 'count msg)))
           (save-excursion
             (goto-char pos)
             (delete-char count))
           ;; Adjust point if delete was before cursor
           (when (< pos old-point)
             (goto-char (max pos (- old-point count))))))
        ("sync_complete"
         (let ((room-length (alist-get 'length msg))
               (content (alist-get 'content msg))
               (push-mode elva--push-buffer-on-sync))
           (setq elva--push-buffer-on-sync nil)
           (cond
            ;; Reconnect mode: restore our saved content if room is empty
            ((eq push-mode 'always)
             (if (> room-length 0)
                 ;; Room has content, we'll use it (already synced)
                 (message "Elva: reconnected, using room content")
               ;; Room is empty, restore our saved content
               (when (and elva--saved-content (> (length elva--saved-content) 0))
                 (elva--send `((op . "insert") (pos . 0) (text . ,elva--saved-content)))
                 (message "Elva: reconnected and restored buffer to room")))
             (setq elva--saved-content nil))
            ;; elva-room-from-buffer mode: fail if room has content
            ((eq push-mode 'if-empty)
             (if (> room-length 0)
                 (progn
                   (message "Elva: room already has content (%d chars), not overwriting" room-length)
                   (elva-disconnect))
               ;; Room is empty, send our buffer content
               (let ((buf-content (buffer-string)))
                 (when (> (length buf-content) 0)
                   (elva--send `((op . "insert") (pos . 0) (text . ,buf-content)))
                   (message "Elva: pushed buffer to room"))))))
           (force-mode-line-update)))
        ("error"
         (let ((error-msg (alist-get 'message msg)))
           (message "Elva error: %s" error-msg)))))))

(defun elva--sentinel (proc event buffer)
  "Handle process PROC state change EVENT for BUFFER."
  (let ((status (string-trim event)))
    (when (and (buffer-live-p buffer)
               (not (process-live-p proc)))
      (with-current-buffer buffer
        (setq elva--process nil)
        (remove-hook 'after-change-functions #'elva--after-change t)
        (force-mode-line-update)
        ;; Attempt reconnection if enabled and not manually disconnected
        (if (and elva--url
                 (> elva-max-reconnect-attempts 0)
                 (< elva--reconnect-count elva-max-reconnect-attempts))
            (progn
              (cl-incf elva--reconnect-count)
              (message "Elva: connection lost. Reconnecting in %.1fs... (attempt %d/%d)"
                       elva-reconnect-delay
                       elva--reconnect-count
                       elva-max-reconnect-attempts)
              (setq elva--reconnect-timer
                    (run-at-time elva-reconnect-delay nil
                                 #'elva--try-reconnect buffer)))
          ;; No more attempts or reconnection disabled
          (if elva--url
              (message "Elva: connection lost after %d attempts" elva--reconnect-count)
            (message "Elva: disconnected")))))))

(defun elva--try-reconnect (buffer)
  "Attempt to reconnect BUFFER to Elva."
  (when (buffer-live-p buffer)
    (with-current-buffer buffer
      (setq elva--reconnect-timer nil)
      ;; Save buffer content to restore if room is empty
      (let ((saved-content (buffer-string)))
        (setq elva--saved-content saved-content))
      ;; Clear buffer to avoid duplication when room syncs
      (let ((inhibit-modification-hooks t))
        (erase-buffer))
      (setq elva--push-buffer-on-sync 'always)
      (condition-case err
          (elva--do-connect elva--url)
        (error
         (message "Elva: reconnection failed: %s" err)
         ;; Schedule another attempt if we have retries left
         (when (< elva--reconnect-count elva-max-reconnect-attempts)
           (cl-incf elva--reconnect-count)
           (setq elva--reconnect-timer
                 (run-at-time elva-reconnect-delay nil
                              #'elva--try-reconnect buffer))))))))

(defun elva-disconnect ()
  "Disconnect current buffer from Elva."
  (interactive)
  ;; Clear URL to prevent reconnection (but keep room-id for modeline)
  (setq elva--url nil)
  ;; Cancel reconnection timer
  (when elva--reconnect-timer
    (cancel-timer elva--reconnect-timer)
    (setq elva--reconnect-timer nil))
  ;; Flush any pending changes before disconnecting
  (elva--flush-changes)
  ;; Cancel batch timer
  (when elva--batch-timer
    (cancel-timer elva--batch-timer)
    (setq elva--batch-timer nil))
  ;; Kill the process
  (when elva--process
    (delete-process elva--process)
    (setq elva--process nil))
  (remove-hook 'after-change-functions #'elva--after-change t)
  (force-mode-line-update)
  (message "Disconnected from Elva"))

(defun elva-status ()
  "Show connection status for current buffer."
  (interactive)
  (cond
   ((and elva--process (process-live-p elva--process))
    (message "Elva: connected to %s" elva--url))
   (elva--reconnect-timer
    (message "Elva: reconnecting to %s (attempt %d/%d)"
             elva--url elva--reconnect-count elva-max-reconnect-attempts))
   (elva--url
    (message "Elva: disconnected from %s" elva--url))
   (t
    (message "Elva: not connected"))))

(provide 'elva)
;;; elva.el ends here
