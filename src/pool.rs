pub use crate::forkserver::template_main;
use crate::{
    bridge::{self, Bridge, Reply},
    capture::{self, Calls},
    config::Config,
    forkserver::Template,
    policy::{parse_duration, Grants, Policy, Program},
    preload::TemplateConfig,
    session::{Session, Step},
    worker::Job,
};
use anyhow::{anyhow, bail, Context, Result};
use clap::Parser;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashMap},
    io::{BufRead, Write},
    path::PathBuf,
    sync::{
        mpsc::{self, Receiver, RecvTimeoutError, SyncSender},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};

const DEFAULT_SESSION_LIMIT: usize = 16;
const DEFAULT_SESSION_IDLE_MS: u64 = 600_000;

const MAX_REQUEST: usize = 1_048_576;

#[cfg(test)]
#[path = "../tests/rust/pool.rs"]
mod tests;

#[derive(Parser)]
#[command(
    name = "pynox pool",
    about = "Persistent CPython pool; one JSON request per stdin line"
)]
pub struct PoolCli {
    /// Maximum concurrent executions; each slot has a clean CPython template (default 2).
    #[arg(long, value_parser = clap::value_parser!(u16).range(1..=32))]
    workers: Option<u16>,
    /// Preload trusted modules before forking jobs (comma-separated, repeatable).
    #[arg(long, value_delimiter = ',')]
    preload: Vec<String>,
    /// Trusted package directory, readable by every job in this pool.
    #[arg(long, value_name = "PATH")]
    package_dir: Vec<PathBuf>,
    /// pynox.toml with [pool] settings, [pool.limits] and [profiles]
    /// (default: $PYNOX_CONFIG, then ./pynox.toml).
    #[arg(long, value_name = "PATH")]
    config: Option<PathBuf>,
    /// Structured job logs on stderr: "json" or "off" (default off).
    #[arg(long, value_name = "FORMAT")]
    log: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    #[serde(default)]
    id: Value,
    /// Answer with the pool's counters instead of running anything.
    #[serde(default)]
    stats: bool,
    /// Answer a host call: `{"call_id": n, "result": ...}` or `{"call_id": n, "error": "..."}`.
    #[serde(default)]
    reply: Option<Reply>,
    /// Host functions the code may call through `pynox_host`.
    #[serde(default)]
    functions: Vec<String>,
    /// Run in a persistent session of this name: one isolated process whose
    /// `__main__` survives between requests. Created by the first request,
    /// which fixes its permissions; later requests only carry `code`.
    #[serde(default)]
    session: Option<String>,
    /// With `session`, end it instead of running code.
    #[serde(default)]
    close: bool,
    /// Named profile from the pool's configuration; request fields widen it.
    #[serde(default)]
    profile: Option<String>,
    /// Path of a script file to run. Exactly one of `script` and `code` is required.
    #[serde(default)]
    script: Option<PathBuf>,
    /// Inline program text; it needs no file and grants no file access.
    #[serde(default)]
    code: Option<String>,
    /// Display name for inline code in tracebacks and errors (default `<code>`).
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    args: Vec<String>,
    #[serde(default)]
    allow_network: Option<bool>,
    #[serde(default)]
    allow_read: Vec<PathBuf>,
    #[serde(default)]
    allow_write: Vec<PathBuf>,
    #[serde(default)]
    package_dirs: Vec<PathBuf>,
    #[serde(default)]
    env: BTreeMap<String, String>,
    #[serde(default)]
    timeout: Option<String>,
    #[serde(default)]
    memory: Option<String>,
    #[serde(default)]
    max_output: Option<u64>,
    #[serde(default)]
    max_file_size: Option<String>,
}

impl Request {
    /// A step for an existing session: inline code only, no permission fields.
    fn step_overrides(&self) -> Result<(Option<u64>, Option<usize>)> {
        if self.code.is_none() {
            bail!("Session steps take inline code");
        }
        if self.script.is_some()
            || self.profile.is_some()
            || self.allow_network.is_some()
            || self.memory.is_some()
            || self.max_file_size.is_some()
            || !self.functions.is_empty()
            || !self.allow_read.is_empty()
            || !self.allow_write.is_empty()
            || !self.package_dirs.is_empty()
            || !self.env.is_empty()
        {
            bail!(
                "Session permissions are fixed by its first request; later steps carry only code"
            );
        }
        let timeout = match &self.timeout {
            Some(value) => Some(parse_duration(value).map_err(|e| anyhow!(e))?),
            None => None,
        };
        Ok((timeout, self.max_output.map(|n| n as usize)))
    }

    fn job(self, config: Option<&Config>) -> Result<Job> {
        let program = match (self.script, self.code) {
            (Some(script), None) => {
                if self.name.is_some() {
                    bail!("name applies to code requests only");
                }
                Program::Script {
                    path: script.into_os_string(),
                    args: self.args,
                }
            }
            (None, Some(source)) => {
                let label = self.name.unwrap_or_else(|| "<code>".into());
                Program::Code {
                    source,
                    argv0: label.clone(),
                    label,
                    args: self.args,
                }
            }
            (Some(_), Some(_)) => bail!("Use either script or code, not both"),
            (None, None) => bail!("Request needs script or code"),
        };
        let mut grants = match &self.profile {
            Some(profile) => config
                .context("profile needs a pool started with --config")?
                .profile(profile)?
                .clone(),
            None => Grants::default(),
        };
        grants = grants.overlay(&Grants {
            allow_read: self.allow_read,
            allow_write: self.allow_write,
            allow_env: self.env.keys().cloned().collect(),
            package_dirs: self.package_dirs,
            allow_network: self.allow_network,
            timeout: self.timeout,
            memory: self.memory,
            max_output: self.max_output,
            max_file_size: self.max_file_size,
        });
        // Requests are capped by [pool.limits]; the operator sets the ceiling.
        let mut policy = Policy::new(&grants, program, true)?;
        if let Some(config) = config {
            config.check_limits(&mut policy)?;
        }
        bridge::check_names(&self.functions)?;
        policy.host_functions = self.functions;
        if self.env.values().any(|value| value.contains('\0')) {
            bail!("Environment values cannot contain NUL");
        }
        let mut env: BTreeMap<String, String> = [
            ("LANG".into(), "C.UTF-8".into()),
            ("OPENBLAS_NUM_THREADS".into(), "1".into()),
            ("OMP_NUM_THREADS".into(), "1".into()),
        ]
        .into_iter()
        .collect();
        env.extend(self.env);
        Ok(Job {
            policy,
            env,
            session: false,
        })
    }
}

type Sessions = Arc<Mutex<HashMap<String, SyncSender<Pending>>>>;

/// Drive one session: create it on the first request, run steps, end it on
/// `close`, a fatal step, idle timeout or pool shutdown.
#[allow(clippy::too_many_arguments)]
fn serve_session(
    name: String,
    receiver: Receiver<Pending>,
    writer: Writer,
    config: Arc<TemplateConfig>,
    settings: Arc<Option<Config>>,
    sessions: Sessions,
    idle: Duration,
    bridge: Arc<Bridge>,
) -> Result<()> {
    let emit_event = |event: &Value| emit(&writer, event);
    let mut session: Option<Session> = None;
    loop {
        let pending = match receiver.recv_timeout(idle) {
            Ok(pending) => pending,
            Err(RecvTimeoutError::Timeout) => {
                // The dispatcher sends while holding the map, so a request
                // cannot slip in between this check and the removal.
                let mut map = sessions.lock().unwrap();
                match receiver.try_recv() {
                    Ok(pending) => {
                        drop(map);
                        pending
                    }
                    Err(_) => {
                        map.remove(&name);
                        break;
                    }
                }
            }
            Err(RecvTimeoutError::Disconnected) => break,
        };
        let queued_us = pending.received.elapsed().as_micros();
        let request = pending.request;
        let mut result = if request.close {
            // End the process but keep serving the name: a later request starts
            // a fresh session, and the thread leaves on the idle timeout.
            session = None;
            json!({"exit": 0, "error": null, "stdout": "", "stderr": "", "closed": true})
        } else if let Some(live) = session.as_mut() {
            match request.step_overrides() {
                Err(error) => failure(pending.id.clone(), "RequestError", format!("{error:#}")),
                Ok((timeout, max_output)) => {
                    let code = request.code.clone().unwrap_or_default();
                    let step = Step {
                        code: &code,
                        name: request.name.as_deref().unwrap_or("agent.py"),
                        args: &request.args,
                    };
                    let timeout = timeout.unwrap_or(live.policy.timeout_ms);
                    let max_output = max_output.unwrap_or(live.policy.max_output);
                    let calls = Calls {
                        bridge: &bridge,
                        task_id: pending.id.clone(),
                        emit: &emit_event,
                    };
                    match live.step(&step, timeout, max_output, Some(calls)) {
                        Ok(result) => result,
                        Err(error) => {
                            failure(pending.id.clone(), "PoolWorkerError", format!("{error:#}"))
                                .tap_insert("session_ended", json!(true))
                        }
                    }
                }
            }
        } else if request.code.is_none() {
            failure(
                pending.id.clone(),
                "RequestError",
                "Sessions take inline code".into(),
            )
        } else {
            let code = request.code.clone().unwrap_or_default();
            let step_name = request.name.clone().unwrap_or_else(|| "agent.py".into());
            let args = request.args.clone();
            let job = request.job(settings.as_ref().as_ref()).and_then(|mut job| {
                config.apply_to_job(&mut job.policy)?;
                job.session = true;
                Ok(job)
            });
            match job {
                Err(error) => failure(pending.id.clone(), "RequestError", format!("{error:#}")),
                Ok(job) => match Session::start(&config, &job) {
                    Err(error) => {
                        failure(pending.id.clone(), "PoolWorkerError", format!("{error:#}"))
                    }
                    Ok(mut started) => {
                        let step = Step {
                            code: &code,
                            name: &step_name,
                            args: &args,
                        };
                        let timeout = started.policy.timeout_ms;
                        let max_output = started.policy.max_output;
                        let calls = Calls {
                            bridge: &bridge,
                            task_id: pending.id.clone(),
                            emit: &emit_event,
                        };
                        match started.step(&step, timeout, max_output, Some(calls)) {
                            Ok(result) => {
                                if started.alive() {
                                    session = Some(started);
                                }
                                result
                            }
                            Err(error) => {
                                failure(pending.id.clone(), "PoolWorkerError", format!("{error:#}"))
                            }
                        }
                    }
                },
            }
        };
        if session.as_ref().is_some_and(|live| !live.alive()) {
            session = None;
        }
        result["id"] = pending.id;
        result["session"] = json!(name);
        result["queue_us"] = json!(queued_us);
        result["runtime"]["preloaded_modules"] = json!(config.modules);
        emit(&writer, &result)?;
    }
    Ok(())
}

trait TapInsert {
    fn tap_insert(self, key: &str, value: Value) -> Value;
}

impl TapInsert for Value {
    fn tap_insert(mut self, key: &str, value: Value) -> Value {
        self[key] = value;
        self
    }
}

struct Pending {
    id: Value,
    request: Request,
    received: Instant,
}

/// Pool output: protocol lines on stdout, counters for `{"stats": true}`,
/// and optional structured job logs on stderr for operators.
pub struct Output {
    stdout: Mutex<std::io::Stdout>,
    stats: Mutex<Stats>,
    log: bool,
    started: Instant,
}

#[derive(Default)]
struct Stats {
    jobs: u64,
    ok: u64,
    failed: u64,
    timeouts: u64,
    signals: u64,
    request_errors: u64,
    worker_errors: u64,
    duration_ms: u64,
    queue_us: u64,
    errors: BTreeMap<String, u64>,
}

type Writer = Arc<Output>;

impl Output {
    fn new(log: bool) -> Self {
        Output {
            stdout: Mutex::new(std::io::stdout()),
            stats: Mutex::new(Stats::default()),
            log,
            started: Instant::now(),
        }
    }

    /// Count a job result and, with logging on, describe it on stderr. Logs
    /// carry identifiers, outcome and timing, never captured output.
    fn record(&self, result: &Value) {
        let kind = result["error"]["type"].as_str();
        let exit = result["exit"].as_i64().unwrap_or(125);
        {
            let mut stats = self.stats.lock().unwrap();
            stats.jobs += 1;
            stats.duration_ms += result["duration_ms"].as_u64().unwrap_or(0);
            stats.queue_us += result["queue_us"].as_u64().unwrap_or(0);
            match (exit, kind) {
                (0, None) => stats.ok += 1,
                (124, Some("TimeoutError")) => stats.timeouts += 1,
                (_, Some("SignalError")) => stats.signals += 1,
                (_, Some("RequestError")) => stats.request_errors += 1,
                (_, Some("PoolWorkerError" | "SandboxSetupError" | "WorkerError")) => {
                    stats.worker_errors += 1
                }
                _ => stats.failed += 1,
            }
            if let Some(kind) = kind {
                *stats.errors.entry(kind.to_owned()).or_default() += 1;
            }
        }
        if self.log {
            let message: String = result["error"]["message"]
                .as_str()
                .unwrap_or("")
                .chars()
                .take(200)
                .collect();
            let line = json!({
                "ts_ms": std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map_or(0, |d| d.as_millis() as u64),
                "event": "job",
                "id": result["id"], "session": result["session"], "step": result["step"],
                "exit": exit, "error": kind, "message": if kind.is_some() { json!(message) } else { Value::Null },
                "duration_ms": result["duration_ms"], "queue_us": result["queue_us"],
                "network": result["policy"]["network"],
                "truncated": result["stdout_truncated"].as_bool().unwrap_or(false)
                    || result["stderr_truncated"].as_bool().unwrap_or(false),
            });
            let mut stderr = std::io::stderr().lock();
            let _ = writeln!(stderr, "{line}");
        }
    }

    fn snapshot(&self, sessions: usize, workers: u16) -> Value {
        let stats = self.stats.lock().unwrap();
        json!({
            "event": "stats",
            "uptime_ms": self.started.elapsed().as_millis() as u64,
            "workers": workers, "sessions_active": sessions,
            "jobs": stats.jobs, "ok": stats.ok, "failed": stats.failed,
            "timeouts": stats.timeouts, "signals": stats.signals,
            "request_errors": stats.request_errors, "worker_errors": stats.worker_errors,
            "duration_ms_total": stats.duration_ms, "queue_us_total": stats.queue_us,
            "errors": stats.errors,
        })
    }
}

/// Write one protocol line. Job results (those with an `exit` and no
/// `event`) are also counted and, when enabled, logged.
fn emit(writer: &Writer, result: &Value) -> Result<()> {
    if result.get("event").is_none() && result.get("exit").is_some() {
        writer.record(result);
    }
    let stdout = writer.stdout.lock().unwrap();
    crate::runner::write_json_line(stdout.lock(), result)
}

fn failure(id: Value, kind: &str, message: String) -> Value {
    json!({"id": id, "exit": 125, "error": {"type": kind, "message": message},
        "stdout": "", "stderr": ""})
}

pub fn run(options: PoolCli) -> Result<()> {
    let settings: Arc<Option<Config>> = Arc::new(Config::discover(options.config.as_deref())?);
    let section = settings.as_ref().as_ref().map(|c| &c.pool);
    let workers = options
        .workers
        .or(section.and_then(|p| p.workers))
        .unwrap_or(2);
    if !(1..=32).contains(&workers) {
        bail!("pool.workers must be between 1 and 32");
    }
    let preload = if options.preload.is_empty() {
        section.map(|p| p.preload.clone()).unwrap_or_default()
    } else {
        options.preload
    };
    let mut package_dirs = options.package_dir;
    package_dirs.extend(section.iter().flat_map(|p| p.package_dirs.iter().cloned()));
    let config = Arc::new(TemplateConfig::new(preload, package_dirs)?);
    let log = match options
        .log
        .as_deref()
        .or(section.and_then(|p| p.log.as_deref()))
    {
        None | Some("off") => false,
        Some("json") => true,
        Some(other) => bail!("log must be \"off\" or \"json\", not {other:?}"),
    };
    let writer: Writer = Arc::new(Output::new(log));
    // Templates are separate exec'd processes. The threaded supervisor is
    // never forked and cannot copy previous requests/secrets into a new job.
    let templates = (0..workers)
        .map(|_| Template::start(&config))
        .collect::<Result<Vec<_>>>()?;
    let session_limit = section
        .and_then(|p| p.max_sessions)
        .map_or(DEFAULT_SESSION_LIMIT, usize::from);
    let session_idle =
        Duration::from_millis(match section.and_then(|p| p.session_idle.as_deref()) {
            Some(value) => parse_duration(value).map_err(|e| anyhow!(e))?,
            None => DEFAULT_SESSION_IDLE_MS,
        });
    let sessions: Sessions = Arc::new(Mutex::new(HashMap::new()));
    let bridge = Arc::new(Bridge::default());
    let session_threads: Arc<Mutex<Vec<thread::JoinHandle<Result<()>>>>> =
        Arc::new(Mutex::new(Vec::new()));
    let (sender, receiver) = mpsc::sync_channel::<Pending>(workers as usize * 2);
    let receiver = Arc::new(Mutex::new(receiver));
    let threads: Vec<_> = templates
        .into_iter()
        .map(|template| {
            let receiver = Arc::clone(&receiver);
            let writer = Arc::clone(&writer);
            let config = Arc::clone(&config);
            let settings = Arc::clone(&settings);
            let bridge = Arc::clone(&bridge);
            thread::spawn(move || -> Result<()> {
                let emit_event = |event: &Value| emit(&writer, event);
                let mut slot = Some(template);
                loop {
                    let pending = match receiver.lock().unwrap().recv() {
                        Ok(request) => request,
                        Err(_) => break,
                    };
                    let queued_us = pending.received.elapsed().as_micros();
                    let job =
                        pending
                            .request
                            .job(settings.as_ref().as_ref())
                            .and_then(|mut job| {
                                config.apply_to_job(&mut job.policy)?;
                                Ok(job)
                            });
                    let mut result = match job {
                        Err(error) => {
                            failure(pending.id.clone(), "RequestError", format!("{error:#}"))
                        }
                        Ok(job) => {
                            let outcome = (|| -> Result<Value> {
                                let payload = serde_json::to_vec(&job)?;
                                if payload.len() > MAX_REQUEST {
                                    bail!("Request payload exceeds 1 MiB");
                                }
                                if slot.is_none() {
                                    slot = Some(Template::start(&config)?);
                                }
                                let calls = Calls {
                                    bridge: &bridge,
                                    task_id: pending.id.clone(),
                                    emit: &emit_event,
                                };
                                capture::run(
                                    slot.as_mut().unwrap(),
                                    &job.policy,
                                    &payload,
                                    Some(calls),
                                )
                            })();
                            match outcome {
                                Ok(result) => result,
                                Err(error) => {
                                    // Do not retry a task whose side effects might have occurred.
                                    // Replace the broken template before the next request.
                                    slot = None;
                                    failure(
                                        pending.id.clone(),
                                        "PoolWorkerError",
                                        format!("{error:#}"),
                                    )
                                }
                            }
                        }
                    };
                    result["id"] = pending.id;
                    result["queue_us"] = json!(queued_us);
                    result["runtime"]["preloaded_modules"] = json!(config.modules);
                    emit(&writer, &result)?;
                }
                Ok(())
            })
        })
        .collect();
    emit(
        &writer,
        &json!({"event": "ready", "workers": workers, "runtime": "pynox",
            "preloaded_modules": config.modules, "package_dirs": config.package_dirs,
            "config": settings.as_ref().as_ref().map(|c| c.path.clone()),
            "profiles": settings.as_ref().as_ref()
                .map(|c| c.profiles.keys().cloned().collect::<Vec<_>>()).unwrap_or_default(),
            "sessions": {"max": session_limit, "idle_ms": session_idle.as_millis() as u64}}),
    )?;
    let input_result = (|| -> Result<()> {
        let mut input = std::io::stdin().lock();
        let mut sequence = 0u64;
        loop {
            let Some((bytes, oversized)) = read_line(&mut input)? else {
                break;
            };
            sequence += 1;
            let received = Instant::now();
            if oversized {
                emit(
                    &writer,
                    &failure(
                        json!(sequence),
                        "RequestError",
                        "Request exceeds 1 MiB".into(),
                    ),
                )?;
                continue;
            }
            if bytes.iter().all(u8::is_ascii_whitespace) {
                continue;
            }
            // Recover the ID even for unknown fields or invalid option types.
            let raw: Result<Value, _> = serde_json::from_slice(&bytes);
            let id = raw
                .as_ref()
                .ok()
                .and_then(|v| v.get("id"))
                .cloned()
                .unwrap_or(json!(sequence));
            match raw.and_then(serde_json::from_value::<Request>) {
                Ok(mut request) => {
                    request.id = id.clone();
                    if let Some(reply) = &request.reply {
                        if let Err(error) = bridge.reply(reply) {
                            emit(&writer, &failure(id, "RequestError", format!("{error:#}")))?;
                        }
                        continue;
                    }
                    if request.stats {
                        let mut snapshot = writer.snapshot(sessions.lock().unwrap().len(), workers);
                        snapshot["id"] = id;
                        emit(&writer, &snapshot)?;
                        continue;
                    }
                    let pending = Pending {
                        id: id.clone(),
                        request,
                        received,
                    };
                    let Some(name) = pending.request.session.clone() else {
                        sender
                            .send(pending)
                            .map_err(|_| anyhow!("Pool workers stopped"))?;
                        continue;
                    };
                    // Route to the session's own thread; hold the map while
                    // sending so an idle session cannot vanish underneath.
                    let mut map = sessions.lock().unwrap();
                    let session_sender = match map.get(&name) {
                        Some(existing) => existing.clone(),
                        None if pending.request.close => {
                            drop(map);
                            emit(
                                &writer,
                                &failure(id, "RequestError", format!("Unknown session {name:?}")),
                            )?;
                            continue;
                        }
                        None if map.len() >= session_limit => {
                            drop(map);
                            emit(
                                &writer,
                                &failure(
                                    id,
                                    "RequestError",
                                    format!("Session limit reached ({session_limit})"),
                                ),
                            )?;
                            continue;
                        }
                        None => {
                            let (session_sender, session_receiver) =
                                mpsc::sync_channel::<Pending>(8);
                            map.insert(name.clone(), session_sender.clone());
                            let handle = thread::spawn({
                                let (name, writer, config, settings, sessions, bridge) = (
                                    name.clone(),
                                    Arc::clone(&writer),
                                    Arc::clone(&config),
                                    Arc::clone(&settings),
                                    Arc::clone(&sessions),
                                    Arc::clone(&bridge),
                                );
                                move || {
                                    serve_session(
                                        name,
                                        session_receiver,
                                        writer,
                                        config,
                                        settings,
                                        sessions,
                                        session_idle,
                                        bridge,
                                    )
                                }
                            });
                            session_threads.lock().unwrap().push(handle);
                            session_sender
                        }
                    };
                    if session_sender.try_send(pending).is_err() {
                        drop(map);
                        emit(
                            &writer,
                            &failure(id, "RequestError", format!("Session {name:?} is busy")),
                        )?;
                    }
                }
                Err(error) => emit(&writer, &failure(id, "RequestError", error.to_string()))?,
            }
        }
        Ok(())
    })();
    drop(sender);
    sessions.lock().unwrap().clear();
    for thread in threads {
        thread
            .join()
            .map_err(|_| anyhow!("Pool thread panicked"))??;
    }
    let handles = std::mem::take(&mut *session_threads.lock().unwrap());
    for handle in handles {
        handle
            .join()
            .map_err(|_| anyhow!("Session thread panicked"))??;
    }
    input_result
}

fn read_line(input: &mut impl BufRead) -> Result<Option<(Vec<u8>, bool)>> {
    let mut bytes = Vec::new();
    let mut oversized = false;
    let mut consumed_any = false;
    loop {
        let available = input.fill_buf()?;
        if available.is_empty() {
            return Ok(consumed_any.then_some((bytes, oversized)));
        }
        consumed_any = true;
        let end = available.iter().position(|byte| *byte == b'\n');
        let n = end.map_or(available.len(), |position| position + 1);
        if !oversized {
            if bytes.len() + n > MAX_REQUEST {
                oversized = true;
                bytes.clear();
            } else {
                bytes.extend_from_slice(&available[..n]);
            }
        }
        input.consume(n);
        if end.is_some() {
            return Ok(Some((bytes, oversized)));
        }
    }
}
