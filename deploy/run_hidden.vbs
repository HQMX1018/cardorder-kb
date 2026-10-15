' CardOrder KB worker: run run_worker.cmd with a hidden window (0) so it cannot be closed by accident.
CreateObject("WScript.Shell").Run "cmd.exe /c D:\kb-worker\run_worker.cmd", 0, True
