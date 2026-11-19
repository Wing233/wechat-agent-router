"""Connect rust-anything's temporary Python test binding to the Rust bridge code."""

import json
import os
from pathlib import Path
import sys

PORT = '''
use serde_json::Value;

#[path = "APP/src/client.rs"]
mod client;
#[path = "APP/src/json.rs"]
#[allow(dead_code)]
pub(crate) mod json;
#[path = "APP/src/readonly.rs"]
pub(crate) mod readonly;

fn music_error(error: client::MusicError) -> String {
    let _details = (&error.code, error.status);
    format!("MusicError: {}", error.message)
}

pub fn extract_json(text: String) -> Result<Value, String> {
    Ok(client::extract_json(&text).unwrap_or(Value::Null))
}

pub fn resolve_binary(binary: String) -> Result<String, String> {
    client::resolve_binary(&binary)
        .map(|path| path.to_string_lossy().into_owned())
        .map_err(music_error)
}

pub fn build_command(binary: String, args: Vec<String>) -> Result<Vec<String>, String> {
    let path = resolve_binary(binary)?;
    let args: Vec<_> = args.iter().map(String::as_str).collect();
    Ok(std::iter::once(path)
        .chain(client::cli_args(&args).map(str::to_owned))
        .collect())
}

pub fn run_ncm_cli(binary: String, args: Vec<String>, timeout: i64) -> Result<Value, String> {
    let args: Vec<_> = args.iter().map(String::as_str).collect();
    client::run(&binary, &args, timeout.max(1) as u64).map_err(music_error)
}

pub fn validate_args(args: Value) -> Result<Vec<String>, String> {
    readonly::validate_args(Some(&args))
        .map(|args| args.into_iter().map(str::to_owned).collect())
        .map_err(|error| format!("ReadonlyError: {error}"))
}
'''


def verify_unit(command, oracle, app, fixture, *, offline=False):
    unit = oracle / 'rust/music-rs'
    command('rust-anything', 'bridge', oracle, 'plugins/music/')
    # Bind the custom Python exception names in the disposable test wrapper.
    # The generated wrapper normally converts unknown exceptions to ValueError.
    lib = unit / 'src/lib.rs'
    source = lib.read_text(encoding='utf-8')
    source = source.replace('pub mod port;', '''pub mod port;
use port::{json, readonly};
pyo3::create_exception!(_native, MusicError, pyo3::exceptions::PyException);
pyo3::create_exception!(_native, ReadonlyError, pyo3::exceptions::PyValueError);
''')
    source = source.replace('match kind {', '''match kind {
        "MusicError" => MusicError::new_err(text),
        "ReadonlyError" => ReadonlyError::new_err(text),''', 1)
    lib.write_text(source, encoding='utf-8')
    (unit / 'src/port.rs').write_text(
        PORT.replace('APP', app.resolve().as_posix()), encoding='utf-8')
    manifest = unit / 'Cargo.toml'
    cargo = manifest.read_text(encoding='utf-8').replace('edition = "2024"', 'edition = "2021"')
    cargo = cargo.replace('"preserve_order", "float_roundtrip"',
                          '"preserve_order", "float_roundtrip", "arbitrary_precision"')
    manifest.write_text(cargo, encoding='utf-8')

    missing = str(fixture.with_name('not-installed'))
    groups = {
        'extract_json': [[value] for value in (
            '', 'null', 'true', 'hello', '\ufeff{"z":1,"a":"晴天🎵"}',
            'warning {"a":1} {"b":2}', 'invalid [a] [{"x":1}]', r'[{"x":"[\"}"}]',
            '1.25', '警告 {"z": ["晴天🎵"], "a": 5}',
        )],
        'resolve_binary': [[str(fixture)], [sys.executable], [missing]],
        'build_command': [[str(fixture), ['search','song','晴天🎵']], [missing, []]],
        'run_ncm_cli': [[str(fixture), ['search','song', 'fixture-' + name], 2] for name in (
            'warning', 'array', 'null', 'config', 'quota', 'login', 'api', 'code-float',
        )] + [[missing, ['user','info'], 2]],
        'validate_args': [[value] for value in (
            None, 1, {'a': 'b'}, ['search'], ['search','song',0], ['playlist','create'],
            ['search','song','晴天🎵'], ['search','song','--output','json'],
            ['search','song','--limit','201'], ['search','song','--limit','2_0'],
            ['search','song','--offset','-1'], ['search','song','\u001f'],
        )],
    }
    inputs = unit / 'inputs.jsonl'
    inputs.write_text(''.join(json.dumps({'fn': name, 'args': args}, ensure_ascii=False) + '\n'
                              for name, values in groups.items() for args in values), encoding='utf-8')
    env = {**os.environ, 'PYO3_PYTHON': sys.executable, 'CARGO_INCREMENTAL': '0'}
    env.setdefault('CARGO_BUILD_JOBS', '2')
    if offline:
        env['CARGO_NET_OFFLINE'] = 'true'
    if os.name == 'nt':
        # rust-anything selects python3 on a successful spawn, even if the
        # Microsoft Store launcher exits with 9009 instead of running Python.
        paths = os.environ['PATH'].split(os.pathsep)
        env['PATH'] = os.pathsep.join(path for path in paths if Path(path).name.lower() != 'windowsapps')
    command('rust-anything', 'record', unit, '--count', '0', '--inputs', inputs, env=env)
    command('cargo', 'fmt', '--manifest-path', manifest)
    command('rust-anything', 'shadow', unit, env=env)
    gate = command('rust-anything', 'gate', unit, '--json', report=True, env=env)
    (unit / 'gate-report.json').write_text(json.dumps(gate, indent=2), encoding='utf-8')
    print('rust-anything unit gate passed', flush=True)
