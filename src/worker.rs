//! Sandboxed task execution inside a cold worker or a pool template's child.
//! Task data crosses into Python as objects built with the C API, so the
//! bootstrap imports nothing beyond what CPython already initialized.
use crate::{
    policy::{python_info, Policy, PythonInfo},
    preload::TemplateConfig,
    sandbox,
};
use anyhow::{bail, Context, Result};
use pyo3_ffi as py;
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    ffi::{CStr, CString},
    fs::File,
    io::{BufRead, BufReader, Read},
    mem::MaybeUninit,
    os::fd::{AsRawFd, FromRawFd, IntoRawFd},
    path::Path,
    ptr,
};

#[derive(serde::Serialize, serde::Deserialize)]
pub struct Job {
    pub policy: Policy,
    pub env: BTreeMap<String, String>,
    /// Keep the process alive and run newline-delimited steps on descriptor 4.
    #[serde(default)]
    pub session: bool,
}

#[derive(serde::Deserialize)]
struct StepRequest {
    #[serde(default)]
    code: String,
    #[serde(default = "default_step_name")]
    name: String,
    #[serde(default)]
    close: bool,
}

fn default_step_name() -> String {
    "agent.py".into()
}

const REPORT_FD: libc::c_int = 3;

pub fn run(warm: bool) -> i32 {
    match execute(warm) {
        Ok(code) => code,
        Err(error) => {
            let report = json!({
                "exit": 125,
                "error": {"type": "SandboxSetupError", "message": format!("{error:#}")}
            });
            write_report(&report, false);
            125
        }
    }
}

/// Write the diagnostic report without closing the descriptor: a later setup
/// failure must still be able to report on the same channel. Sessions send
/// one report per step, newline-delimited.
fn write_report(report: &Value, newline: bool) {
    let mut payload = report.to_string().into_bytes();
    if newline {
        payload.push(b'\n');
    }
    while !payload.is_empty() {
        let written = unsafe { libc::write(REPORT_FD, payload.as_ptr().cast(), payload.len()) };
        if written < 0 {
            if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return;
        }
        payload.drain(..written as usize);
    }
}

fn execute(warm: bool) -> Result<i32> {
    let mut input = String::new();
    let mut control: Option<BufReader<File>> = None;
    let (policy, env, session) = if warm {
        // Pool children receive the job as the first line on stdin, then lose
        // stdin. Sessions keep reading steps from a copy on descriptor 4.
        if unsafe { libc::dup2(0, 4) } < 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        let mut reader = BufReader::new(unsafe { File::from_raw_fd(4) });
        reader.by_ref().take(1_048_576).read_line(&mut input)?;
        let null = File::open("/dev/null")?;
        if unsafe { libc::dup2(null.as_raw_fd(), 0) } < 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        let job: Job = serde_json::from_str(&input)?;
        if job.session {
            control = Some(reader);
        } else if !job.policy.host_functions.is_empty() {
            // Host replies arrive on descriptor 4; keep it open for the
            // bootstrap's `pynox_host` module. Nothing else is buffered yet.
            let _ = reader.into_inner().into_raw_fd();
        }
        (job.policy, Some(job.env), job.session)
    } else {
        // Cold workers receive the policy on descriptor 4; stdin is whatever the
        // supervisor chose: the user's terminal in passthrough mode, /dev/null with --json.
        let mut pipe = unsafe { File::from_raw_fd(4) };
        pipe.by_ref().take(1_048_576).read_to_string(&mut input)?;
        drop(pipe);
        (serde_json::from_str(&input)?, None, false)
    };
    let python = python_info();
    // No caller-provided descriptors beyond stdio, the report and a session's
    // control channel are exposed to user code.
    let keeps_control = session || (warm && !policy.host_functions.is_empty());
    let first_closed = if keeps_control { 5u32 } else { 4u32 };
    if unsafe { libc::syscall(libc::SYS_close_range, first_closed, u32::MAX, 0u32) } != 0 {
        bail!("close_range failed: {}", std::io::Error::last_os_error());
    }
    sandbox::apply(&policy, &python)?;
    // Cold runs initialize after sandboxing. Pool children inherit trusted
    // CPython startup and optional operator-selected imports; no job code or
    // job data is ever loaded by their template.
    if !warm {
        initialize(&python)?;
    }
    if let Some(env) = env {
        unsafe {
            let result = call_bootstrap(c"_pynox_env", vec![string_dict(&env)?])?;
            py::Py_DECREF(result);
        }
    }
    if let Some(control) = control {
        return run_session(control, &policy, &python);
    }
    let report = unsafe {
        let optional = |text: &Option<String>| -> Result<*mut py::PyObject> {
            match text {
                Some(text) => unicode(text),
                None => Ok(py::Py_NewRef(py::Py_None())),
            }
        };
        let arguments = vec![
            unicode(utf8(&policy.script)?)?,
            unicode(&policy.argv0)?,
            string_list(policy.args.iter().map(String::as_str))?,
            path_list(&policy.package_dirs)?,
            path_list(&python.site_packages)?,
            optional(&policy.source)?,
            optional(&policy.module)?,
            py::PyBool_FromLong(policy.capture as libc::c_long),
            string_list(policy.host_functions.iter().map(String::as_str))?,
        ];
        let result = call_bootstrap(c"_pynox_run", arguments)?;
        let report = to_json(result);
        py::Py_DECREF(result);
        report
    };
    let exit = report
        .get(0)
        .and_then(Value::as_u64)
        .filter(|code| *code <= 255);
    let report = match exit {
        Some(exit) if report.get(1).is_some_and(|e| e.is_null() || e.is_object()) => {
            json!({"exit": exit, "error": report[1]})
        }
        _ => bail!("Bootstrap returned an invalid report"),
    };
    write_report(&report, false);
    let code = unsafe { py::Py_FinalizeEx() };
    if code < 0 {
        bail!("Python finalization failed");
    }
    Ok(0)
}

/// Keep one `__main__` alive and run each step the supervisor sends until it
/// closes the session or the control channel ends.
fn run_session(mut control: BufReader<File>, policy: &Policy, python: &PythonInfo) -> Result<i32> {
    // The session replaces `__main__` with the user's module; keep the step
    // function from the bootstrap's namespace before that happens.
    let step_function = unsafe { bootstrap_function(c"_pynox_session_step")? };
    unsafe {
        let arguments = vec![
            unicode(utf8(&policy.script)?)?,
            unicode(&policy.argv0)?,
            string_list(policy.args.iter().map(String::as_str))?,
            path_list(&policy.package_dirs)?,
            path_list(&python.site_packages)?,
            py::PyBool_FromLong(policy.capture as libc::c_long),
            string_list(policy.host_functions.iter().map(String::as_str))?,
        ];
        let started = call_bootstrap(c"_pynox_session_start", arguments)?;
        py::Py_DECREF(started);
    }
    let mut line = String::new();
    loop {
        line.clear();
        if control.by_ref().take(1_048_576).read_line(&mut line)? == 0 {
            break;
        }
        let step: StepRequest = match serde_json::from_str(&line) {
            Ok(step) => step,
            Err(error) => {
                write_report(
                    &json!({"exit": 125, "error": {"type": "RequestError", "message": format!("Invalid step: {error}")}}),
                    true,
                );
                continue;
            }
        };
        if step.close {
            break;
        }
        let report = unsafe {
            let result = call_function(
                step_function,
                vec![unicode(&step.code)?, unicode(&step.name)?],
            )?;
            let report = to_json(result);
            py::Py_DECREF(result);
            report
        };
        let exit = report
            .get(0)
            .and_then(Value::as_u64)
            .filter(|code| *code <= 255);
        let report = match exit {
            Some(exit) if report.get(1).is_some_and(|e| e.is_null() || e.is_object()) => {
                json!({"exit": exit, "error": report[1]})
            }
            _ => bail!("Session step returned an invalid report"),
        };
        write_report(&report, true);
    }
    let code = unsafe { py::Py_FinalizeEx() };
    if code < 0 {
        bail!("Python finalization failed");
    }
    Ok(0)
}

pub fn initialize(python: &PythonInfo) -> Result<()> {
    unsafe {
        crate::runtime::register_native()?;
        // The isolated configuration does not read the locale, which would
        // leave open(), stdout and the filesystem encoding in ASCII. UTF-8
        // mode matches what stock CPython selects under the launcher's
        // C.UTF-8 environment.
        let mut preconfig = MaybeUninit::<py::PyPreConfig>::uninit();
        py::PyPreConfig_InitIsolatedConfig(preconfig.as_mut_ptr());
        let mut preconfig = preconfig.assume_init();
        preconfig.utf8_mode = 1;
        check(py::Py_PreInitialize(&preconfig))?;
        let mut config = MaybeUninit::<py::PyConfig>::uninit();
        py::PyConfig_InitIsolatedConfig(config.as_mut_ptr());
        let mut config = config.assume_init();
        config.site_import = 0;
        config.write_bytecode = 0;
        config.install_signal_handlers = 0;
        config.buffered_stdio = 1;
        let home = CString::new(utf8(&python.home)?)?;
        let executable = CString::new(utf8(&python.executable)?)?;
        let initialize = (|| -> Result<()> {
            check(py::PyConfig_SetBytesString(
                &mut config,
                &raw mut config.home,
                home.as_ptr(),
            ))?;
            check(py::PyConfig_SetBytesString(
                &mut config,
                &raw mut config.executable,
                executable.as_ptr(),
            ))?;
            check(py::Py_InitializeFromConfig(&config))
        })();
        py::PyConfig_Clear(&mut config);
        initialize?;
    }
    unsafe {
        let main = py::PyImport_ImportModule(c"__main__".as_ptr());
        if main.is_null() {
            bail!("Cannot access the bootstrap namespace");
        }
        let outcome = crate::runtime::run_code(
            crate::runtime::BOOTSTRAP,
            py::PyModule_GetDict(main),
            "Worker bootstrap",
        );
        py::Py_DECREF(main);
        outcome?;
    }
    crate::runtime::install_optimizations()
}

pub fn preload(config: &TemplateConfig) -> Result<()> {
    if config.modules.is_empty() {
        return Ok(());
    }
    let directories = serde_json::to_string(&serde_json::to_string(&config.package_dirs)?)?;
    let modules = serde_json::to_string(&serde_json::to_string(&config.modules)?)?;
    // Flush trusted import output to the template's /dev/null before children
    // inherit the buffers and remap stdout/stderr to their capture pipes.
    python_code(&format!(
        "import sys, json\n\
         sys.path.extend(json.loads({directories}))\n\
         import importlib, gc\n\
         for _preload_name in json.loads({modules}): importlib.import_module(_preload_name)\n\
         gc.collect()\n\
         gc.freeze()\n\
         sys.stdout.flush()\n\
         sys.stderr.flush()\n"
    ))
    .with_context(|| format!("Could not preload {}", config.modules.join(", ")))?;
    crate::preload::check_template()
}

fn python_code(source: &str) -> Result<()> {
    let code = CString::new(source)?;
    let status = unsafe { py::PyRun_SimpleString(code.as_ptr()) };
    if status != 0 {
        bail!("Python bootstrap failed");
    }
    Ok(())
}

fn utf8(path: &Path) -> Result<&str> {
    path.to_str()
        .with_context(|| format!("Non-UTF-8 path: {}", path.display()))
}

unsafe fn unicode(text: &str) -> Result<*mut py::PyObject> {
    let object = py::PyUnicode_FromStringAndSize(text.as_ptr().cast(), text.len() as isize);
    if object.is_null() {
        bail!("Cannot create Python string");
    }
    Ok(object)
}

unsafe fn string_list<'a>(
    items: impl ExactSizeIterator<Item = &'a str>,
) -> Result<*mut py::PyObject> {
    let list = py::PyList_New(items.len() as isize);
    if list.is_null() {
        bail!("Cannot create Python list");
    }
    for (index, item) in items.enumerate() {
        match unicode(item) {
            Ok(object) => py::PyList_SET_ITEM(list, index as isize, object),
            Err(error) => {
                py::Py_DECREF(list);
                return Err(error);
            }
        }
    }
    Ok(list)
}

unsafe fn path_list(paths: &[std::path::PathBuf]) -> Result<*mut py::PyObject> {
    let items = paths.iter().map(|p| utf8(p)).collect::<Result<Vec<_>>>()?;
    string_list(items.into_iter())
}

unsafe fn string_dict(map: &BTreeMap<String, String>) -> Result<*mut py::PyObject> {
    let dict = py::PyDict_New();
    if dict.is_null() {
        bail!("Cannot create Python dict");
    }
    for (name, value) in map {
        let key = unicode(name)?;
        let item = unicode(value)?;
        let status = py::PyDict_SetItem(dict, key, item);
        py::Py_DECREF(key);
        py::Py_DECREF(item);
        if status < 0 {
            py::Py_DECREF(dict);
            bail!("Cannot populate Python dict");
        }
    }
    Ok(dict)
}

/// A trusted function defined by the bootstrap in `__main__`, as a new
/// reference. Look functions up before user code replaces `__main__`.
unsafe fn bootstrap_function(name: &CStr) -> Result<*mut py::PyObject> {
    let main = py::PyImport_ImportModule(c"__main__".as_ptr());
    if main.is_null() {
        bail!("Cannot access bootstrap module");
    }
    let function = py::PyObject_GetAttrString(main, name.as_ptr());
    py::Py_DECREF(main);
    if function.is_null() {
        py::PyErr_Clear();
        bail!("Bootstrap function {} is missing", name.to_string_lossy());
    }
    Ok(function)
}

/// Call a trusted bootstrap function by name. Arguments are owned references
/// and are consumed; the result is a new reference.
unsafe fn call_bootstrap(
    name: &CStr,
    arguments: Vec<*mut py::PyObject>,
) -> Result<*mut py::PyObject> {
    let function = bootstrap_function(name)?;
    let result = call_function(function, arguments);
    py::Py_DECREF(function);
    result
}

/// Call `function` (a borrowed reference) with owned arguments, consuming them.
unsafe fn call_function(
    function: *mut py::PyObject,
    arguments: Vec<*mut py::PyObject>,
) -> Result<*mut py::PyObject> {
    let tuple = py::PyTuple_New(arguments.len() as isize);
    for (index, argument) in arguments.into_iter().enumerate() {
        py::PyTuple_SET_ITEM(tuple, index as isize, argument);
    }
    let result = py::PyObject_Call(function, tuple, ptr::null_mut());
    py::Py_DECREF(tuple);
    if result.is_null() {
        py::PyErr_Print();
        bail!("Python bootstrap failed");
    }
    Ok(result)
}

/// Convert the bootstrap's plain-data report to JSON. The GIL is held and the
/// object is owned by the caller; unexpected types fall back to their repr.
unsafe fn to_json(object: *mut py::PyObject) -> Value {
    if object == py::Py_None() {
        return Value::Null;
    }
    if py::PyBool_Check(object) != 0 {
        return Value::Bool(object == py::Py_True());
    }
    if py::PyLong_Check(object) != 0 {
        let mut overflow = 0;
        let number = py::PyLong_AsLongLongAndOverflow(object, &mut overflow);
        if overflow == 0 && py::PyErr_Occurred().is_null() {
            return json!(number);
        }
        py::PyErr_Clear();
        return repr(object);
    }
    if py::PyUnicode_Check(object) != 0 {
        return text(object).map_or(Value::Null, Value::String);
    }
    if py::PyDict_Check(object) != 0 {
        let mut map = serde_json::Map::new();
        let mut position = 0;
        let mut key = ptr::null_mut();
        let mut value = ptr::null_mut();
        while py::PyDict_Next(object, &mut position, &mut key, &mut value) != 0 {
            let name = match to_json(key) {
                Value::String(name) => name,
                other => other.to_string(),
            };
            map.insert(name, to_json(value));
        }
        return Value::Object(map);
    }
    if py::PyList_Check(object) != 0 || py::PyTuple_Check(object) != 0 {
        let length = py::PySequence_Size(object);
        let mut items = Vec::new();
        for index in 0..length.max(0) {
            let item = py::PySequence_GetItem(object, index);
            if item.is_null() {
                py::PyErr_Clear();
                items.push(Value::Null);
                continue;
            }
            items.push(to_json(item));
            py::Py_DECREF(item);
        }
        return Value::Array(items);
    }
    repr(object)
}

unsafe fn text(object: *mut py::PyObject) -> Option<String> {
    let mut size = 0;
    let pointer = py::PyUnicode_AsUTF8AndSize(object, &mut size);
    if pointer.is_null() {
        py::PyErr_Clear();
        return None;
    }
    let bytes = std::slice::from_raw_parts(pointer.cast::<u8>(), size as usize);
    Some(String::from_utf8_lossy(bytes).into_owned())
}

unsafe fn repr(object: *mut py::PyObject) -> Value {
    let representation = py::PyObject_Repr(object);
    if representation.is_null() {
        py::PyErr_Clear();
        return Value::Null;
    }
    let value = text(representation).map_or(Value::Null, Value::String);
    py::Py_DECREF(representation);
    value
}

unsafe fn check(status: py::PyStatus) -> Result<()> {
    if py::PyStatus_Exception(status) != 0 {
        let detail = if status.err_msg.is_null() {
            "unknown initialization error".into()
        } else {
            CStr::from_ptr(status.err_msg)
                .to_string_lossy()
                .into_owned()
        };
        bail!("CPython initialization failed: {detail}");
    }
    Ok(())
}
