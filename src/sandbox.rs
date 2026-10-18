use crate::policy::{Policy, PythonInfo};
use anyhow::{bail, Context, Result};
use landlock::{
    path_beneath_rules, Access, AccessFs, CompatLevel, Compatible, Ruleset, RulesetAttr,
    RulesetCreatedAttr, RulesetStatus, ABI,
};
use seccompiler::{
    BpfProgram, SeccompAction, SeccompCmpArgLen, SeccompCmpOp, SeccompCondition, SeccompFilter,
    SeccompRule,
};
use std::{collections::BTreeMap, path::PathBuf};

pub fn apply(policy: &Policy, python: &PythonInfo) -> Result<()> {
    limits(policy)?;
    filesystem(policy, python)
        .context("Landlock unavailable or policy could not be enforced (ABI >= 3 required)")?;
    syscalls(policy.allow_network).context("Cannot install seccomp filter")?;
    Ok(())
}

fn limits(policy: &Policy) -> Result<()> {
    for (resource, maximum) in [
        (libc::RLIMIT_AS, policy.memory_bytes),
        (libc::RLIMIT_CPU, policy.timeout_ms.div_ceil(1000) + 1),
        (libc::RLIMIT_NOFILE, 128),
        (libc::RLIMIT_CORE, 0),
    ] {
        let limit = libc::rlimit {
            rlim_cur: maximum,
            rlim_max: maximum,
        };
        if unsafe { libc::setrlimit(resource, &limit) } != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
    }
    if unsafe { libc::prctl(libc::PR_SET_DUMPABLE, 0, 0, 0, 0) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(())
}

fn filesystem(policy: &Policy, python: &PythonInfo) -> Result<()> {
    // ABI 3 adds truncate mediation. Never silently accept partial enforcement.
    let abi = ABI::V3;
    let read = AccessFs::ReadFile | AccessFs::ReadDir;
    let write = AccessFs::WriteFile
        | AccessFs::Truncate
        | AccessFs::MakeReg
        | AccessFs::MakeDir
        | AccessFs::RemoveFile
        | AccessFs::RemoveDir;
    let mut readable = vec![python.stdlib.clone(), python.platstdlib.clone()];
    if policy.source.is_none() && policy.module.is_none() {
        readable.push(policy.script.clone());
    }
    readable.extend(policy.implicit_read.iter().cloned());
    readable.extend(python.site_packages.iter().filter(|p| p.exists()).cloned());
    // Dynamic extensions need the system shared libraries. These are part of the
    // trusted runtime, and are explicitly disclosed as implicit read permissions.
    readable.extend(
        [
            "/lib",
            "/lib64",
            "/usr/lib",
            "/usr/lib64",
            "/etc/ld.so.cache",
            "/dev/urandom",
        ]
        .into_iter()
        .map(PathBuf::from)
        .filter(|p| p.exists()),
    );
    readable.extend(policy.allow_read.iter().cloned());
    readable.extend(policy.package_dirs.iter().cloned());
    if policy.allow_network {
        readable.extend(
            [
                "/etc/resolv.conf",
                "/etc/hosts",
                "/etc/nsswitch.conf",
                "/etc/gai.conf",
                "/etc/ssl/certs",
                "/etc/pki/tls/certs",
                "/etc/pki/ca-trust",
            ]
            .into_iter()
            .map(PathBuf::from)
            .filter(|p| p.exists()),
        );
    }
    let status = Ruleset::default()
        .set_compatibility(CompatLevel::HardRequirement)
        .handle_access(AccessFs::from_all(abi))?
        .create()?
        .add_rules(path_beneath_rules(&readable, read))?
        .add_rules(path_beneath_rules(&policy.allow_write, write))?
        .restrict_self()?;
    if status.ruleset != RulesetStatus::FullyEnforced {
        bail!("Landlock did not fully enforce the requested policy");
    }
    Ok(())
}

// Adapted from Moby's default seccomp profile, v28.0.4 (Apache-2.0):
// https://github.com/moby/moby/blob/6430e49a55babd9b8f4d08e70ecb2b68900770fe/profiles/seccomp/default.json
// Keep only native x86_64/aarch64 runtime operations. Unlike a container, this
// worker has no private namespaces: omit process creation, host IPC, metadata
// mutation, capability/identity changes and privileged kernel interfaces.
// Landlock still mediates every allowed filesystem operation. New syscalls must
// be reviewed and explicitly added; a kernel upgrade cannot widen this policy.
fn allowed_syscalls() -> BTreeMap<i64, Vec<SeccompRule>> {
    let allowed = [
        // Buffered/file-descriptor I/O, imports, SQLite and extension loading.
        libc::SYS_read,
        libc::SYS_write,
        libc::SYS_readv,
        libc::SYS_writev,
        libc::SYS_pread64,
        libc::SYS_pwrite64,
        libc::SYS_preadv,
        libc::SYS_pwritev,
        libc::SYS_preadv2,
        libc::SYS_pwritev2,
        libc::SYS_close,
        libc::SYS_close_range,
        libc::SYS_lseek,
        libc::SYS_dup,
        libc::SYS_dup3,
        libc::SYS_fcntl,
        libc::SYS_flock,
        libc::SYS_ioctl,
        libc::SYS_pipe2,
        libc::SYS_sendfile,
        libc::SYS_copy_file_range,
        libc::SYS_readahead,
        libc::SYS_fadvise64,
        libc::SYS_fallocate,
        libc::SYS_fsync,
        libc::SYS_fdatasync,
        libc::SYS_sync_file_range,
        libc::SYS_ftruncate,
        libc::SYS_truncate,
        libc::SYS_openat,
        libc::SYS_openat2,
        libc::SYS_newfstatat,
        libc::SYS_statx,
        libc::SYS_fstat,
        libc::SYS_fstatfs,
        libc::SYS_statfs,
        libc::SYS_faccessat,
        libc::SYS_faccessat2,
        libc::SYS_getdents64,
        libc::SYS_getcwd,
        libc::SYS_chdir,
        libc::SYS_fchdir,
        libc::SYS_readlinkat,
        libc::SYS_mkdirat,
        libc::SYS_unlinkat,
        libc::SYS_renameat,
        libc::SYS_renameat2,
        libc::SYS_getxattr,
        libc::SYS_lgetxattr,
        libc::SYS_fgetxattr,
        libc::SYS_listxattr,
        libc::SYS_llistxattr,
        libc::SYS_flistxattr,
        // Allocators, dynamic libraries and libc thread bookkeeping.
        libc::SYS_brk,
        libc::SYS_mmap,
        libc::SYS_mprotect,
        libc::SYS_munmap,
        libc::SYS_mremap,
        libc::SYS_madvise,
        libc::SYS_mincore,
        libc::SYS_msync,
        libc::SYS_membarrier,
        libc::SYS_futex,
        libc::SYS_futex_waitv,
        libc::SYS_set_robust_list,
        libc::SYS_set_tid_address,
        libc::SYS_rseq,
        libc::SYS_sched_yield,
        libc::SYS_sched_getaffinity,
        libc::SYS_sched_getparam,
        libc::SYS_sched_getscheduler,
        libc::SYS_sched_get_priority_max,
        libc::SYS_sched_get_priority_min,
        // Signal handling is needed by Python and pthreads; sending signals to
        // other processes (including kill/tgkill) is deliberately absent.
        libc::SYS_rt_sigaction,
        libc::SYS_rt_sigprocmask,
        libc::SYS_rt_sigreturn,
        libc::SYS_rt_sigpending,
        libc::SYS_rt_sigtimedwait,
        libc::SYS_rt_sigsuspend,
        libc::SYS_sigaltstack,
        libc::SYS_restart_syscall,
        libc::SYS_exit,
        libc::SYS_exit_group,
        // Clocks, randomness and read-only runtime/process information.
        libc::SYS_clock_gettime,
        libc::SYS_clock_getres,
        libc::SYS_clock_nanosleep,
        libc::SYS_nanosleep,
        libc::SYS_gettimeofday,
        libc::SYS_times,
        libc::SYS_getitimer,
        libc::SYS_setitimer,
        libc::SYS_getrandom,
        libc::SYS_getcpu,
        libc::SYS_getpid,
        libc::SYS_getppid,
        libc::SYS_gettid,
        libc::SYS_getuid,
        libc::SYS_geteuid,
        libc::SYS_getgid,
        libc::SYS_getegid,
        libc::SYS_getgroups,
        libc::SYS_getresuid,
        libc::SYS_getresgid,
        libc::SYS_getpgid,
        libc::SYS_getsid,
        libc::SYS_getrlimit,
        libc::SYS_getrusage,
        libc::SYS_uname,
        libc::SYS_sysinfo,
        libc::SYS_umask,
        // Event loops and existing descriptors, including asyncio's unnamed
        // stream socketpair. Socket creation/connection is granted below.
        libc::SYS_epoll_create1,
        libc::SYS_epoll_ctl,
        libc::SYS_epoll_pwait,
        libc::SYS_epoll_pwait2,
        libc::SYS_ppoll,
        libc::SYS_pselect6,
        libc::SYS_eventfd2,
        libc::SYS_timerfd_create,
        libc::SYS_timerfd_gettime,
        libc::SYS_timerfd_settime,
        libc::SYS_signalfd4,
        libc::SYS_sendto,
        libc::SYS_recvfrom,
        libc::SYS_sendmsg,
        libc::SYS_recvmsg,
        libc::SYS_sendmmsg,
        libc::SYS_recvmmsg,
        libc::SYS_getsockopt,
        libc::SYS_setsockopt,
        libc::SYS_getsockname,
        libc::SYS_getpeername,
        libc::SYS_shutdown,
        // The earlier compatibility filter always returns ENOSYS for clone3.
        // Allow here only so this layer does not replace ENOSYS with EPERM.
        libc::SYS_clone3,
    ];
    #[allow(unused_mut)]
    let mut rules: BTreeMap<_, _> = allowed.into_iter().map(|nr| (nr, vec![])).collect();
    // x86_64 libc also uses these legacy entry points; aarch64 only has the
    // corresponding *at, *1/*2 and pselect/ppoll interfaces above.
    #[cfg(target_arch = "x86_64")]
    for nr in [
        libc::SYS_access,
        libc::SYS_open,
        libc::SYS_stat,
        libc::SYS_lstat,
        libc::SYS_poll,
        libc::SYS_select,
        libc::SYS_pipe,
        libc::SYS_dup2,
        libc::SYS_creat,
        libc::SYS_readlink,
        libc::SYS_mkdir,
        libc::SYS_rmdir,
        libc::SYS_unlink,
        libc::SYS_rename,
        libc::SYS_getdents,
        libc::SYS_epoll_create,
        libc::SYS_epoll_wait,
        libc::SYS_eventfd,
        libc::SYS_signalfd,
        libc::SYS_time,
        libc::SYS_arch_prctl,
    ] {
        rules.insert(nr, vec![]);
    }
    rules
}

fn compile_filter(
    rules: BTreeMap<i64, Vec<SeccompRule>>,
    default: SeccompAction,
    matched: SeccompAction,
) -> Result<BpfProgram> {
    Ok(
        SeccompFilter::new(rules, default, matched, std::env::consts::ARCH.try_into()?)?
            .try_into()?,
    )
}

// Build without applying: unit tests evaluate the actual BPF and its stacked
// actions without restricting the test runner. Production installs every layer
// before initializing CPython or running any task code.
fn syscall_filters(allow_network: bool) -> Result<Vec<BpfProgram>> {
    let mut rules = allowed_syscalls();
    rules.insert(
        libc::SYS_socketpair,
        [
            libc::SOCK_STREAM,
            libc::SOCK_STREAM | libc::SOCK_CLOEXEC,
            libc::SOCK_STREAM | libc::SOCK_NONBLOCK,
            libc::SOCK_STREAM | libc::SOCK_CLOEXEC | libc::SOCK_NONBLOCK,
        ]
        .into_iter()
        .map(|kind| {
            SeccompRule::new(vec![
                SeccompCondition::new(
                    0,
                    SeccompCmpArgLen::Dword,
                    SeccompCmpOp::Eq,
                    libc::AF_UNIX as u64,
                )?,
                SeccompCondition::new(1, SeccompCmpArgLen::Dword, SeccompCmpOp::Eq, kind as u64)?,
            ])
        })
        .collect::<std::result::Result<_, _>>()?,
    );
    if allow_network {
        rules.insert(
            libc::SYS_socket,
            [libc::AF_INET, libc::AF_INET6]
                .into_iter()
                .map(|family| {
                    SeccompRule::new(vec![SeccompCondition::new(
                        0,
                        SeccompCmpArgLen::Dword,
                        SeccompCmpOp::Eq,
                        family as u64,
                    )?])
                })
                .collect::<std::result::Result<_, _>>()?,
        );
        for nr in [
            libc::SYS_connect,
            libc::SYS_bind,
            libc::SYS_listen,
            libc::SYS_accept,
            libc::SYS_accept4,
        ] {
            rules.insert(nr, vec![]);
        }
    }
    // Preserve the existing thread-only clone contract on both native ABIs.
    rules.insert(
        libc::SYS_clone,
        vec![SeccompRule::new(vec![SeccompCondition::new(
            0,
            SeccompCmpArgLen::Qword,
            SeccompCmpOp::MaskedEq(libc::CLONE_THREAD as u64),
            libc::CLONE_THREAD as u64,
        )?])?],
    );
    // Native libraries may query their own limits, but never change them or
    // query another process. Arguments are pid_t (32 bits) and a pointer (64).
    rules.insert(
        libc::SYS_prlimit64,
        vec![SeccompRule::new(vec![
            SeccompCondition::new(0, SeccompCmpArgLen::Dword, SeccompCmpOp::Eq, 0)?,
            SeccompCondition::new(2, SeccompCmpArgLen::Qword, SeccompCmpOp::Eq, 0)?,
        ])?],
    );
    // Python 3.14+ sets native thread names through pthread_setname_np/prctl.
    // No unrestricted prctl or seccomp permission is needed after installation.
    rules.insert(
        libc::SYS_prctl,
        [
            libc::PR_SET_NAME,
            libc::PR_GET_NAME,
            libc::PR_GET_DUMPABLE,
            libc::PR_GET_SECCOMP,
            libc::PR_GET_NO_NEW_PRIVS,
        ]
        .into_iter()
        .map(|option| {
            SeccompRule::new(vec![SeccompCondition::new(
                0,
                SeccompCmpArgLen::Dword,
                SeccompCmpOp::Eq,
                option as u64,
            )?])
        })
        .collect::<std::result::Result<_, _>>()?,
    );
    // Like Moby, keep clone3 unavailable so glibc can fall back to clone.
    // Install this compatibility layer first; the final allowlist denies
    // further seccomp/prctl filter installation in the worker.
    let mut filters = vec![compile_filter(
        [(libc::SYS_clone3, vec![])].into_iter().collect(),
        SeccompAction::Allow,
        SeccompAction::Errno(libc::ENOSYS as u32),
    )?];
    #[cfg(target_arch = "x86_64")]
    {
        // Preserve the existing x32 ABI rejection contract (SignalError).
        // This is ABI validation, not the default for unknown native syscalls.
        use seccompiler::sock_filter;
        filters.push(vec![
            sock_filter {
                code: 0x20,
                jt: 0,
                jf: 0,
                k: 0,
            }, // LD nr
            sock_filter {
                code: 0x45,
                jt: 0,
                jf: 1,
                k: 0x4000_0000,
            }, // JSET x32
            sock_filter {
                code: 0x06,
                jt: 0,
                jf: 0,
                k: libc::SECCOMP_RET_KILL_PROCESS,
            },
            sock_filter {
                code: 0x06,
                jt: 0,
                jf: 0,
                k: libc::SECCOMP_RET_ALLOW,
            },
        ]);
    }
    filters.push(compile_filter(
        rules,
        SeccompAction::Errno(libc::EPERM as u32),
        SeccompAction::Allow,
    )?);
    Ok(filters)
}

fn syscalls(allow_network: bool) -> Result<()> {
    for program in syscall_filters(allow_network)? {
        seccompiler::apply_filter(&program)?;
    }
    Ok(())
}

#[cfg(test)]
#[path = "../tests/rust/sandbox.rs"]
mod tests;
