;;; elva.el --- Collaborative editing via Elva/Yjs -*- lexical-binding: t -*-

;;; Commentary:
;;
;; This package provides collaborative editing for Emacs via Elva,
;; a Yjs-based collaboration server.  It uses a Python subprocess
;; (elva-bridge.py) that handles Yjs protocol communication.
;;
;; Usage:
;;   M-x elva-connect RET ws://localhost:8000/room/my-room RET
;;
;; Requirements:
;;   - Python 3.8+
;;   - pycrdt: pip install pycrdt
;;   - websockets: pip install websockets

;;; Code:

(require 'json)

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

(defun elva-connect (url)
  "Connect current buffer to Elva server at URL."
  (interactive "sElva URL (e.g., ws://localhost:8000/room/test): ")
  (when elva--process
    (error "Already connected.  Use `elva-disconnect' first"))
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
  ;; Send initial buffer content
  (let ((content (buffer-string)))
    (when (> (length content) 0)
      (elva--send `((op . "insert") (pos . 0) (text . ,content)))))
  ;; Watch for local changes
  (add-hook 'after-change-functions #'elva--after-change nil t)
  (message "Connected to Elva: %s" url))

(defun elva--after-change (beg end old-len)
  "Handle local buffer change between BEG and END.
OLD-LEN is the length of the replaced text."
  (unless elva--applying-remote
    ;; Handle deletion
    (when (> old-len 0)
      (elva--send `((op . "delete")
                    (pos . ,(1- beg))      ; Convert to 0-indexed
                    (count . ,old-len))))
    ;; Handle insertion
    (when (> end beg)
      (let ((text (buffer-substring-no-properties beg end)))
        (elva--send `((op . "insert")
                      (pos . ,(1- beg))    ; Convert to 0-indexed
                      (text . ,text)))))))

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
  "Apply remote change MSG to BUFFER."
  (with-current-buffer buffer
    (let ((elva--applying-remote t)
          (inhibit-modification-hooks t))
      (save-excursion
        (pcase (alist-get 'op msg)
          ("insert"
           (let ((pos (1+ (alist-get 'pos msg)))  ; Convert to 1-indexed
                 (text (alist-get 'text msg)))
             (goto-char pos)
             (insert text)))
          ("delete"
           (let ((pos (1+ (alist-get 'pos msg)))  ; Convert to 1-indexed
                 (count (alist-get 'count msg)))
             (goto-char pos)
             (delete-char count))))))))

(defun elva--sentinel (proc event buffer)
  "Handle process PROC state change EVENT for BUFFER."
  (let ((status (string-trim event)))
    (message "Elva bridge: %s" status)
    (when (and (buffer-live-p buffer)
               (not (process-live-p proc)))
      (with-current-buffer buffer
        (setq elva--process nil)
        (remove-hook 'after-change-functions #'elva--after-change t)))))

(defun elva-disconnect ()
  "Disconnect current buffer from Elva."
  (interactive)
  (when elva--process
    (delete-process elva--process)
    (setq elva--process nil))
  (remove-hook 'after-change-functions #'elva--after-change t)
  (message "Disconnected from Elva"))

(defun elva-status ()
  "Show connection status for current buffer."
  (interactive)
  (if (and elva--process (process-live-p elva--process))
      (message "Elva: connected")
    (message "Elva: not connected")))

(provide 'elva)
;;; elva.el ends here
