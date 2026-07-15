#!/usr/bin/env python3
"""Daemonize a command - macOS-friendly equivalent of `setsid`.

Fork once, child calls os.setsid() to break out of the caller's process
group and become a session leader. The grandchild PPID becomes 1
(launchd), so it's fully detached from any shell or Hermes session that
spawned it.

Usage:
    daemonize.py LOGFILE CMD [ARGS...]

Prints the daemon's PID on stdout. The bash caller captures it into a
.pid file. The Python wrapper itself exits ~10ms later, but the daemon
keeps running under PID it printed.

Flow:
    bash ── exec daemonize.py ──┬─ fork #1 ──┬─ parent: print grandchild PID, exit
                                │             └─ child: setsid() + fork #2 ──┬─ exits
                                │                                          └─ grandchild (daemon)
"""
import os
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print(f"usage: {sys.argv[0]} LOGFILE CMD [ARGS...]", file=sys.stderr)
        return 2

    log_path = sys.argv[1]
    cmd_args = sys.argv[2:]

    # First fork: parent becomes the PID reporter, child becomes the daemon ancestor.
    # Use a pipe so the daemon's grandchild PID (the actual daemon) can flow back up.
    r_fd, w_fd = os.pipe()

    pid = os.fork()
    if pid > 0:
        # Parent (PID reporter): wait for daemon to print its PID, then exit.
        os.close(w_fd)
        # Read the daemon PID from the pipe (up to 32 bytes incl newline)
        daemon_pid_bytes = b""
        while True:
            chunk = os.read(r_fd, 32)
            if not chunk:
                break
            daemon_pid_bytes += chunk
        os.close(r_fd)
        # Print to OUR stdout, which goes back to bash via command substitution.
        try:
            sys.stdout.write(daemon_pid_bytes.decode("utf-8"))
            sys.stdout.flush()
        except Exception:
            pass
        # Reap the immediate child (which already exited via _exit below)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        return 0

    # First child: close read end of pipe, become session leader, fork again.
    os.close(r_fd)
    pid = os.fork()
    if pid > 0:
        # First child: report grandchild PID via pipe, then exit.
        # This decouples the bash caller from the daemon's PID.
        msg = f"{pid}\n".encode("utf-8")
        try:
            os.write(w_fd, msg)
        except OSError:
            pass
        os.close(w_fd)
        os._exit(0)

    # Grandchild (the real daemon): redirect stdio, exec the command.
    os.close(w_fd)

    # stdin ← /dev/null
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)

    # stdout/stderr ← log file (append, create 0644)
    log_fd = os.open(
        log_path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o644,
    )
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(log_fd)

    # Replace the wrapper with the real command. execvp preserves our PID.
    try:
        os.execvp(cmd_args[0], cmd_args)
    except FileNotFoundError:
        # We've already redirected stderr, but try anyway
        print(f"[daemonize] command not found: {cmd_args[0]}", file=sys.stderr)
        os._exit(127)
    except OSError as e:
        print(f"[daemonize] exec failed: {e}", file=sys.stderr)
        os._exit(126)

    # unreachable
    return 0


if __name__ == "__main__":
    sys.exit(main())
