"""Start only experiment-owned loopback subprocesses, then stop them when the command exits."""
from contextlib import ExitStack, contextmanager
from pathlib import Path
import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from corpus import STATE
from retrieval import XERJ, XERJ_PORT, json_request


@contextmanager
def child(name,command,port,health,log_dir=STATE):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1",port))
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir/f"{name}-service.log").open("a") as log:
        proc = subprocess.Popen(command,stdout=log,stderr=log)
        try:
            for _ in range(240):
                if proc.poll() is not None:
                    raise RuntimeError(f"{name} exited; see {log.name}")
                try:
                    json_request(health,timeout=1)
                    break
                except OSError:
                    time.sleep(0.5)
            else:
                raise RuntimeError(f"{name} health timeout")
            yield proc.pid
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--llama",action="store_true")
    p.add_argument("--llama-model", type=Path, default=STATE/"models"/"Qwen3-4B-Q4_K_M.gguf",
                   help="Local GGUF file for an explicitly requested model comparison")
    p.add_argument("--data-dir", type=Path, default=STATE/"xerj")
    p.add_argument("--log-dir", type=Path, default=STATE)
    p.add_argument("command",nargs=argparse.REMAINDER)
    args=p.parse_args()
    command=args.command
    if command[:1]==["--"]:
        command=command[1:]
    if not command:
        p.error("Pass -- followed by a command")
    binaries = STATE/"bin"
    with ExitStack() as stack:
        xerj_pid = stack.enter_context(child("xerj",[binaries/"xerj","--bind","127.0.0.1","--port",str(XERJ_PORT),
            "--insecure","--disable-feedback","--data-dir",args.data_dir],XERJ_PORT,XERJ,args.log_dir))
        usage = shutil.disk_usage(args.data_dir)
        if 1-usage.free/usage.total>=0.94:
            if usage.free < 8*1024**3:
                raise RuntimeError("Less than 8 GiB free for the local benchmark")
            # On a large, nearly full volume the default 95% blocks even this tiny index.
            # This transient setting belongs only to the isolated subprocess above.
            json_request(XERJ+"/_cluster/settings",{"transient":{
                "cluster.routing.allocation.disk.watermark.flood_stage":"99%"}},method="PUT")
        if args.llama:
            stack.enter_context(child("llama",[binaries/"llama-b12541"/"llama-server","-m",
                args.llama_model,"--offline","--host","127.0.0.1","--port",
                "19391","--reasoning","off","-c","8192","-np","1"],19391,"http://127.0.0.1:19391/health",args.log_dir))
        return subprocess.run(command, env={**os.environ, "WIKI_XERJ_PID": str(xerj_pid)}).returncode


if __name__=="__main__":
    sys.exit(main())
