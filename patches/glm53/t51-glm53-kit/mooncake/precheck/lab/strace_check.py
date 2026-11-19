#!/usr/bin/env python3
"""Check an `strace -f -yy` log of the environment check for anything that touches the environment.

Usage: strace_check.py STRACE_LOG OUT_DIR
Allowed: reading files; writing to stdout/stderr; creating and writing the two output files in OUT_DIR
(t51_check_*.json, t51_glm53_diff_*.patch); lowering its own priority. Everything else is reported.
"""
import collections
import os
import re
import sys

LOG, OUT_DIR = sys.argv[1], sys.argv[2].rstrip("/")
LINE = re.compile(r"^(\d+)\s+(\w+)\((.*)\)\s+=\s+(-?\d+|\?|0x[0-9a-f]+)(.*)$")
FD = re.compile(r"^(-?\d+)<([^>]*)>")
OUTPUT = re.compile(re.escape(OUT_DIR) + r"/(t51_check_[0-9a-f]{16}(\.\d+)?\.json|t51_glm53_diff_[0-9a-f]{16}(\.\d+)?\.patch)$")
MODIFY = {"mkdir", "mkdirat", "rmdir", "unlink", "unlinkat", "rename", "renameat", "renameat2", "link", "linkat",
          "symlink", "symlinkat", "chmod", "fchmod", "fchmodat", "fchmodat2", "chown", "fchown", "lchown", "fchownat",
          "truncate", "utime", "utimes", "utimensat", "futimesat", "setxattr", "lsetxattr", "fsetxattr",
          "removexattr", "lremovexattr", "fremovexattr", "mknod", "mknodat", "fallocate", "sync", "syncfs",
          "mount", "umount2", "swapon", "swapoff", "chroot", "pivot_root"}
NETWORK = {"socket", "socketpair", "connect", "bind", "listen", "accept", "accept4", "sendto", "sendmsg",
           "sendmmsg", "recvfrom", "recvmsg", "recvmmsg", "shutdown", "getsockopt", "setsockopt"}
PROCESS = {"clone", "clone3", "fork", "vfork", "execve", "execveat"}
SIGNAL_SEND = {"kill", "tkill", "tgkill", "rt_sigqueueinfo", "rt_tgsigqueueinfo", "pidfd_send_signal"}
OTHER_BAD = {"ptrace", "process_vm_readv", "process_vm_writev", "shmget", "shmat", "msgget", "msgsnd", "semget", "semop"}
WRITES = {"write", "pwrite64", "writev", "pwritev", "pwritev2"}
OPENS = {"open", "openat", "openat2", "creat"}

counts = collections.Counter()
violations = []
opened_for_write = set()
out_fds = set()  # (pid, fd) of the output files, from their own openat; removed on close
read_bytes = collections.Counter()
read_files = set()
execs = []
first_pid = None
with open(LOG, errors="replace") as f:
    for raw in f:
        m = LINE.match(raw.rstrip("\n"))
        if not m:
            continue
        pid, name, args, ret = m.group(1), m.group(2), m.group(3), m.group(4)
        first_pid = first_pid or pid
        counts[name] += 1
        if name in PROCESS:
            execs.append(raw.strip())
            if not (name == "execve" and pid == first_pid and len(execs) == 1):
                violations.append("process: " + raw.strip()[:200])
            continue
        if name in NETWORK or name in SIGNAL_SEND or name in OTHER_BAD or name in MODIFY:
            violations.append(name + ": " + raw.strip()[:200])
            continue
        if name in OPENS:
            path_m = re.search(r'"([^"]*)"', args)
            path = path_m.group(1) if path_m else "?"
            if re.match(r"^/proc/\d+/mem$", path):
                violations.append("proc_mem: " + raw.strip()[:200])
            flags = args
            writing = any(x in flags for x in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND"))
            if writing:
                if int(ret) >= 0 and OUTPUT.search(path):
                    opened_for_write.add(path)
                    out_fds.add((pid, ret))
                else:
                    violations.append("open_for_write: " + raw.strip()[:200])
            elif int(ret) >= 0:
                read_files.add(path)
            continue
        if name == "close":
            fd = FD.match(args)
            if fd:
                out_fds.discard((pid, fd.group(1)))
            continue
        if name in WRITES:
            fd = FD.match(args)
            target = fd.group(2) if fd else "?"
            fdn = fd.group(1) if fd else "?"
            if fdn in ("1", "2") and (target.startswith("pipe:") or target.startswith("/dev/")):
                continue
            if (pid, fdn) in out_fds:
                continue
            violations.append("write: " + raw.strip()[:200])
            continue
        if name in ("ftruncate", "fsync", "fdatasync"):
            fd = FD.match(args)
            if not (fd and (pid, fd.group(1)) in out_fds):
                violations.append(name + ": " + raw.strip()[:200])
            continue
        if name == "mmap":
            if "PROT_WRITE" in args and "MAP_SHARED" in args:
                violations.append("mmap_shared_write: " + raw.strip()[:200])
            continue
        if name == "read":
            fd = FD.match(args)
            if fd and int(ret) > 0:
                read_bytes[fd.group(2)] += int(ret)

print("strace log: %s" % os.path.basename(LOG))
print("syscalls traced: %d (%s)" % (sum(counts.values()), ", ".join("%s=%d" % kv for kv in counts.most_common(14))))
print("process: %s" % ("only the initial execve" if len(execs) == 1 else "%d calls" % len(execs)))
print("network calls: %d" % sum(counts[n] for n in NETWORK))
print("signals sent: %d" % sum(counts[n] for n in SIGNAL_SEND))
print("files opened for writing: %d" % len(opened_for_write))
for p in sorted(opened_for_write):
    print("  " + p)
print("files opened read-only: %d" % len(read_files))
print("bytes read in total: %d; largest:" % sum(read_bytes.values()))
for path, n in read_bytes.most_common(6):
    print("  %12d  %s" % (n, path))
print("priority calls: setpriority=%d ioprio_set=%d" % (counts["setpriority"], counts["ioprio_set"]))
print("violations: %d" % len(violations))
for v in violations[:50]:
    print("  " + v)
sys.exit(1 if violations else 0)
