use super::syscall_filters;
use seccompiler::BpfProgram;

// Evaluate the emitted classic BPF on synthetic seccomp_data. No syscall is
// issued and no filter is installed in this process. Only instructions emitted
// by seccompiler and the existing x32 guard are supported; others fail loudly.
fn evaluate(program: &BpfProgram, data: &[u8; 64]) -> u32 {
    let mut accumulator = 0u32;
    let mut pc = 0usize;
    for _ in 0..program.len() {
        let instruction = &program[pc];
        pc += 1;
        match instruction.code {
            0x20 => {
                let offset = instruction.k as usize;
                accumulator = u32::from_le_bytes(data[offset..offset + 4].try_into().unwrap());
            }
            0x54 => accumulator &= instruction.k,
            0x05 => pc += instruction.k as usize,
            0x15 | 0x25 | 0x35 | 0x45 => {
                let matches = match instruction.code {
                    0x15 => accumulator == instruction.k,
                    0x25 => accumulator > instruction.k,
                    0x35 => accumulator >= instruction.k,
                    0x45 => accumulator & instruction.k != 0,
                    _ => unreachable!(),
                };
                pc += if matches {
                    instruction.jt
                } else {
                    instruction.jf
                } as usize;
            }
            0x06 => return instruction.k,
            other => panic!("unsupported BPF opcode {other:#x}"),
        }
    }
    panic!("filter did not return an action");
}

fn action(filters: &[BpfProgram], nr: i64, args: [u64; 6]) -> u32 {
    let arch: u32 = match std::env::consts::ARCH {
        "x86_64" => 0xc000_003e,
        "aarch64" => 0xc000_00b7,
        other => panic!("unsupported architecture {other}"),
    };
    let mut data = [0u8; 64];
    data[..4].copy_from_slice(&(nr as u32).to_le_bytes());
    data[4..8].copy_from_slice(&arch.to_le_bytes());
    for (index, arg) in args.iter().enumerate() {
        data[16 + index * 8..24 + index * 8].copy_from_slice(&arg.to_le_bytes());
    }
    // Linux chooses the lowest signed action value; for equal actions the
    // newest filter supplies the data (e.g. errno).
    let mut result = libc::SECCOMP_RET_ALLOW;
    for filter in filters {
        let candidate = evaluate(filter, &data);
        if ((candidate & 0xffff_0000) as i32) <= ((result & 0xffff_0000) as i32) {
            result = candidate;
        }
    }
    result
}

const ALLOW: u32 = libc::SECCOMP_RET_ALLOW;
const DENY: u32 = libc::SECCOMP_RET_ERRNO | libc::EPERM as u32;

#[test]
fn runtime_io_memory_threads_and_event_loops_are_allowed() {
    for network in [false, true] {
        let filters = syscall_filters(network).unwrap();
        for nr in [
            libc::SYS_read,
            libc::SYS_write,
            libc::SYS_openat,
            libc::SYS_newfstatat,
            libc::SYS_getdents64,
            libc::SYS_ftruncate,
            libc::SYS_fcntl,
            libc::SYS_mmap,
            libc::SYS_mprotect,
            libc::SYS_futex,
            libc::SYS_set_robust_list,
            libc::SYS_rseq,
            libc::SYS_clock_gettime,
            libc::SYS_getrandom,
            libc::SYS_rt_sigaction,
            libc::SYS_epoll_pwait,
            libc::SYS_recvfrom,
            libc::SYS_sendto,
            libc::SYS_exit_group,
        ] {
            assert_eq!(action(&filters, nr, [0; 6]), ALLOW, "syscall {nr}");
        }
    }
}

#[test]
fn forbidden_families_and_unlisted_native_syscalls_return_eperm() {
    for network in [false, true] {
        let filters = syscall_filters(network).unwrap();
        for nr in [
            libc::SYS_execve,
            libc::SYS_execveat,
            libc::SYS_ptrace,
            libc::SYS_process_vm_readv,
            libc::SYS_process_vm_writev,
            libc::SYS_pidfd_getfd,
            libc::SYS_pidfd_open,
            libc::SYS_kill,
            libc::SYS_tgkill,
            libc::SYS_rt_sigqueueinfo,
            libc::SYS_setrlimit,
            libc::SYS_fchmod,
            libc::SYS_fchown,
            libc::SYS_utimensat,
            libc::SYS_setxattr,
            libc::SYS_removexattr,
            libc::SYS_mount,
            libc::SYS_unshare,
            libc::SYS_setns,
            libc::SYS_bpf,
            libc::SYS_io_uring_setup,
            libc::SYS_userfaultfd,
            libc::SYS_shmget,
            libc::SYS_semget,
            libc::SYS_msgget,
            libc::SYS_mq_open,
            libc::SYS_seccomp,
            // Native calls absent from the old denylist, now denied by default.
            libc::SYS_personality,
            libc::SYS_memfd_create,
            452,    // fchmodat2 on both supported architectures
            999,    // unassigned syscall
            10_000, // future/unknown native syscall
        ] {
            assert_eq!(action(&filters, nr, [0; 6]), DENY, "syscall {nr}");
        }
        #[cfg(target_arch = "x86_64")]
        for nr in [libc::SYS_fork, libc::SYS_vfork, libc::SYS_chmod] {
            assert_eq!(action(&filters, nr, [0; 6]), DENY, "syscall {nr}");
        }
    }
}

#[test]
fn network_permission_only_enables_inet_sockets_and_connection_operations() {
    for network in [false, true] {
        let filters = syscall_filters(network).unwrap();
        for family in [
            libc::AF_INET,
            libc::AF_INET6,
            libc::AF_UNIX,
            libc::AF_NETLINK,
        ] {
            let expected = if network && [libc::AF_INET, libc::AF_INET6].contains(&family) {
                ALLOW
            } else {
                DENY
            };
            assert_eq!(
                action(&filters, libc::SYS_socket, [family as u64, 1, 0, 0, 0, 0]),
                expected
            );
        }
        for nr in [
            libc::SYS_connect,
            libc::SYS_bind,
            libc::SYS_listen,
            libc::SYS_accept,
            libc::SYS_accept4,
        ] {
            assert_eq!(
                action(&filters, nr, [0; 6]),
                if network { ALLOW } else { DENY }
            );
        }
    }
}

#[test]
fn asyncio_socketpair_requires_unix_stream_with_only_supported_flags() {
    for network in [false, true] {
        let filters = syscall_filters(network).unwrap();
        for flags in [
            0,
            libc::SOCK_CLOEXEC,
            libc::SOCK_NONBLOCK,
            libc::SOCK_CLOEXEC | libc::SOCK_NONBLOCK,
        ] {
            for kind in [libc::SOCK_STREAM, libc::SOCK_DGRAM, libc::SOCK_SEQPACKET] {
                for family in [libc::AF_UNIX, libc::AF_INET] {
                    assert_eq!(
                        action(
                            &filters,
                            libc::SYS_socketpair,
                            [family as u64, (kind | flags) as u64, 0, 0, 0, 0]
                        ),
                        if family == libc::AF_UNIX && kind == libc::SOCK_STREAM {
                            ALLOW
                        } else {
                            DENY
                        },
                    );
                }
            }
        }
    }
}

#[test]
fn clone_is_thread_only_and_clone3_preserves_libc_fallback() {
    let filters = syscall_filters(false).unwrap();
    let threads = libc::CLONE_VM
        | libc::CLONE_FS
        | libc::CLONE_FILES
        | libc::CLONE_SIGHAND
        | libc::CLONE_THREAD;
    for flags in [0, libc::SIGCHLD, libc::CLONE_VM, threads] {
        assert_eq!(
            action(&filters, libc::SYS_clone, [flags as u64, 0, 0, 0, 0, 0]),
            if flags == threads { ALLOW } else { DENY },
        );
    }
    assert_eq!(
        action(&filters, libc::SYS_clone3, [0; 6]),
        libc::SECCOMP_RET_ERRNO | libc::ENOSYS as u32,
    );
}

#[test]
fn prlimit_only_queries_own_limits_and_checks_the_full_pointer() {
    let filters = syscall_filters(false).unwrap();
    assert_eq!(
        action(&filters, libc::SYS_prlimit64, [0, 7, 0, 0x1000, 0, 0]),
        ALLOW
    );
    for args in [
        [42, 7, 0, 0x1000, 0, 0],
        [0, 7, 0x1000, 0, 0, 0],
        [0, 7, 1 << 32, 0, 0, 0],
    ] {
        assert_eq!(action(&filters, libc::SYS_prlimit64, args), DENY);
    }
}

#[test]
fn prctl_allows_thread_names_and_queries_but_not_security_changes() {
    let filters = syscall_filters(false).unwrap();
    for option in [
        libc::PR_SET_NAME,
        libc::PR_GET_NAME,
        libc::PR_GET_NO_NEW_PRIVS,
    ] {
        assert_eq!(
            action(&filters, libc::SYS_prctl, [option as u64, 0, 0, 0, 0, 0]),
            ALLOW
        );
    }
    for option in [
        libc::PR_SET_DUMPABLE,
        libc::PR_SET_SECCOMP,
        libc::PR_SET_NO_NEW_PRIVS,
    ] {
        assert_eq!(
            action(&filters, libc::SYS_prctl, [option as u64, 0, 0, 0, 0, 0]),
            DENY
        );
    }
}

#[test]
#[cfg(target_arch = "x86_64")]
fn x32_preserves_the_existing_abi_rejection_contract() {
    let filters = syscall_filters(false).unwrap();
    for nr in [libc::SYS_getpid, libc::SYS_read, 10_000] {
        assert_eq!(
            action(&filters, nr | 0x4000_0000, [0; 6]),
            libc::SECCOMP_RET_KILL_PROCESS
        );
    }
}
