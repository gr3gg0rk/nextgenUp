; NSIS installer hooks for NextGenUp.
; Windows cannot overwrite a running executable, and the sidecar server
; keeps running when an older app version launches this installer.
; Kill the sidecar process tree (/T also stops its ffmpeg children)
; before any files are written.

!macro NSIS_HOOK_PREINSTALL
  nsExec::Exec 'taskkill /F /T /IM nextgenup-server.exe'
  Sleep 800
!macroend
