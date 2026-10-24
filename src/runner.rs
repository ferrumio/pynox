//! Process supervision for one sandboxed task: pass output through like
//! `python`, or capture it into a structured JSON result with `--json`.
use crate::{
    config::Config,
    policy::{python_info, Invocation, Policy, Program},
};
use anyhow::{Context, Result};
use serde_json::{json, Value};
use std::{
    fs::File,
    io::{BufWriter, Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd, RawFd},
        unix::process::{CommandExt, ExitStatusExt},
    },
    process::{Child, Command, ExitStatus, Stdio},
    thread,
    time::{Duration, Instant},
};

#[cfg(test)]
#[path = "../tests/rust/diagnostics.rs"]
mod tests;

fn drain(mut reader: impl Read, maximum: usize) -> (Vec<u8>, bool) {
    let mut captured = Vec::new();
    let mut buffer = [0; 8192];
    let mut truncated = false;
    loop {
        match reader.read(&mut buffer) {
            Ok(0) => break,
            Ok(n) => {
                let keep = n.min(maximum.saturating_sub(captured.len()));
                captured.extend_from_slice(&buffer[..keep]);
                truncated |= keep < n;
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        }
    }
    (captured, truncated)
}

fn pipe() -> Result<(File, File)> {
    let mut fds = [-1; 2];
    if unsafe { libc::pipe2(fds.as_mut_ptr(), libc::O_CLOEXEC) } < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(unsafe { (File::from_raw_fd(fds[0]), File::from_raw_fd(fds[1])) })
}

/// Block until the worker exits or the deadline passes. A pidfd wakes the
/// supervisor immediately, instead of a polling sleep adding latency to
/// every task.
fn wait_until(child: &mut Child, deadline: Instant) -> Result<(ExitStatus, bool)> {
    let pidfd = unsafe { libc::syscall(libc::SYS_pidfd_open, child.id() as libc::pid_t, 0) };
    if pidfd < 0 {
        return Err(std::io::Error::last_os_error()).context("Cannot watch sandbox worker");
    }
    let pidfd = unsafe { File::from_raw_fd(pidfd as RawFd) };
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            let _ = child.kill();
            return Ok((child.wait()?, true));
        }
        let mut poll = libc::pollfd {
            fd: pidfd.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        let timeout = remaining.as_millis().min(i32::MAX as u128 - 1) as i32 + 1;
        let ready = unsafe { libc::poll(&mut poll, 1, timeout) };
        if ready < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error.into());
        }
        if ready > 0 {
            return Ok((child.wait()?, false));
        }
    }
}

/// Resolve configuration, profile and flags into one policy. Command-line
/// flags widen the configured defaults: the person typing them is the operator.
pub fn policy_for(invocation: &Invocation) -> Result<Policy> {
    let config = Config::discover(invocation.config.as_deref())?;
    let grants = match &config {
        Some(config) => config
            .grants(invocation.profile.as_deref())?
            .overlay(&invocation.grants),
        None if invocation.profile.is_some() => {
            anyhow::bail!("--profile needs a pynox.toml (see --config)")
        }
        None => invocation.grants.clone(),
    };
    let program = Program::from_python_args(&invocation.python_args)?;
    Policy::new(&grants, program, invocation.json)
}

/// Run a sandboxed task end to end and return the process exit status.
pub fn execute(invocation: &Invocation, start: Instant) -> i32 {
    match supervise(invocation, start) {
        Ok(result) => report(invocation.json, &result),
        Err(error) => {
            if invocation.json {
                let result = json!({
                    "exit": 125,
                    "error": {"type": "LauncherError", "message": format!("{error:#}")},
                    "stdout": "", "stderr": "", "duration_ms": start.elapsed().as_millis()
                });
                write_json_line(std::io::stdout().lock(), &result)
                    .expect("Cannot write JSON result");
            } else {
                eprintln!("pynox: {error:#}");
            }
            125
        }
    }
}

fn report(json: bool, result: &Value) -> i32 {
    let exit = result["exit"].as_i64().unwrap_or(125) as i32;
    if json {
        write_json_line(std::io::stdout().lock(), result).expect("Cannot write JSON result");
        return exit;
    }
    // Output already went to the terminal. Python exceptions printed their
    // traceback; add the hint, and describe failures that produced no traceback.
    let error = &result["error"];
    if error.is_object() {
        if error["file"].is_null() && error["line"].is_null() {
            eprintln!(
                "pynox: {}: {}",
                error["type"].as_str().unwrap_or("Error"),
                error["message"].as_str().unwrap_or("")
            );
        }
        if let Some(hint) = error["hint"].as_str() {
            eprintln!("pynox: {hint}");
        }
    }
    exit
}

/// Batch the serializer's small writes (notably escaped print newlines), with
/// bounded extra memory. Flush each complete report before returning.
pub fn write_json_line(writer: impl Write, result: &Value) -> Result<()> {
    let mut writer = BufWriter::new(writer);
    serde_json::to_writer(&mut writer, result)?;
    writer.write_all(b"\n")?;
    writer.flush()?;
    Ok(())
}

pub fn supervise(invocation: &Invocation, start: Instant) -> Result<Value> {
    let policy = policy_for(invocation)?;
    let (report_reader, report_writer) = pipe()?;
    let (policy_reader, mut policy_writer) = pipe()?;
    let report_fd = report_writer.as_raw_fd();
    let policy_fd = policy_reader.as_raw_fd();
    let mut command = Command::new(std::env::current_exe()?);
    command
        .arg("--internal-worker")
        .env_clear()
        .env("LANG", "C.UTF-8")
        .env("OPENBLAS_NUM_THREADS", "1")
        .env("OMP_NUM_THREADS", "1");
    if policy.capture {
        command
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
    } else {
        command
            .stdin(Stdio::inherit())
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit());
    }
    for name in &policy.allow_env {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    unsafe {
        command.pre_exec(move || {
            // Descriptor 3 carries the report back; 4 delivers the policy.
            if libc::dup2(report_fd, 3) < 0
                || libc::fcntl(3, libc::F_SETFD, 0) < 0
                || libc::dup2(policy_fd, 4) < 0
                || libc::fcntl(4, libc::F_SETFD, 0) < 0
            {
                return Err(std::io::Error::last_os_error());
            }
            // If the supervisor disappears, do not leave a worker running.
            if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL, 0, 0, 0) < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = command.spawn().context("Cannot start sandbox worker")?;
    drop((report_writer, policy_reader));
    policy_writer.write_all(serde_json::to_string(&policy)?.as_bytes())?;
    drop(policy_writer);
    let maximum = policy.max_output;
    let stdout_task = child
        .stdout
        .take()
        .map(|out| thread::spawn(move || drain(out, maximum)));
    let stderr_task = child
        .stderr
        .take()
        .map(|err| thread::spawn(move || drain(err, maximum)));
    let report_task = thread::spawn(move || drain(report_reader, 65_536));
    let (status, timed_out) =
        wait_until(&mut child, start + Duration::from_millis(policy.timeout_ms))?;
    let collect = |task: Option<thread::JoinHandle<(Vec<u8>, bool)>>| {
        task.map_or((Vec::new(), false), |task| task.join().unwrap())
    };
    let stdout = collect(stdout_task);
    let stderr = collect(stderr_task);
    let report = report_task.join().unwrap();
    Ok(finish(
        &policy,
        start,
        status,
        timed_out,
        [stdout, stderr, report],
    ))
}

pub fn finish(
    policy: &Policy,
    start: Instant,
    status: ExitStatus,
    timed_out: bool,
    output: [(Vec<u8>, bool); 3],
) -> Value {
    let [(stdout, stdout_truncated), (stderr, stderr_truncated), (report, report_truncated)] =
        output;
    let failure = |code, kind, message: String| {
        json!({
            "exit": code, "error": {"type": kind, "message": message}
        })
    };
    let mut result = if timed_out {
        failure(
            124,
            "TimeoutError",
            format!("Wall time exceeded {}ms", policy.timeout_ms),
        )
    } else if let Some(signal) = status.signal() {
        failure(
            128 + signal,
            "SignalError",
            format!("Worker terminated by signal {signal}"),
        )
    } else {
        match serde_json::from_slice::<Value>(&report) {
            Ok(value)
                if !report_truncated
                    && value["exit"].as_u64().is_some_and(|n| n <= 255)
                    && (value["error"].is_null() || value["error"].is_object()) =>
            {
                value
            }
            _ => failure(
                if status.success() {
                    125
                } else {
                    status.code().unwrap_or(125)
                },
                "WorkerError",
                "Worker exited without a valid diagnostic report".into(),
            ),
        }
    };
    result["stdout"] = String::from_utf8_lossy(&stdout).into_owned().into();
    result["stderr"] = String::from_utf8_lossy(&stderr).into_owned().into();
    result["stdout_truncated"] = stdout_truncated.into();
    result["stderr_truncated"] = stderr_truncated.into();
    result["duration_ms"] = json!(start.elapsed().as_millis());
    result["duration_us"] = json!(start.elapsed().as_micros());
    result["runtime"] = json!({
        "name": "pynox", "python": python_info().version,
        "output": if policy.capture { "captured" } else { "passthrough" }
    });
    result["policy"] = json!({
        "allow_read": policy.allow_read, "allow_write": policy.allow_write,
        "implicit_read": policy.implicit_read,
        "package_dirs": policy.package_dirs, "allow_env": policy.allow_env,
        "network": if policy.allow_network { "ipv4+ipv6" } else { "deny" },
        "subprocesses": "deny", "host_unix_sockets": "deny",
        "timeout_ms": policy.timeout_ms, "memory_bytes": policy.memory_bytes,
        "max_file_size_bytes": policy.max_file_size,
        "backend": "landlock+seccomp"
    });
    result
}
