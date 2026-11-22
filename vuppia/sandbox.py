"""Ejecución aislada.

- DockerSandbox: obligatorio para código no confiable (propuesto por IA).
  Sin red, FS de solo lectura, sin capabilities, usuario nobody, límites de
  CPU/RAM/PIDs, kill al vencer el tiempo. El contenedor NO recibe el socket de
  Docker, ni variables de entorno del host, ni credenciales.
- LocalSandbox: solo para código confiable del núcleo (protocolos integrados)
  cuando no hay Docker. Usa unshare -n (sin red) si es posible y rlimits.

El resultado se lee de /out/result.json; stdout se ignora (2.2 confiaba en
stdout y un candidato podía imprimir un puntaje falso).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

RUNNER = r'''
import json, sys, importlib.util
sys.dont_write_bytecode = True
sys.path.insert(0, "/job" if len(sys.argv) < 2 else sys.argv[1])
job = sys.argv[1] if len(sys.argv) > 1 else "/job"
out = sys.argv[2] if len(sys.argv) > 2 else "/out"
with open(job + "/input.json") as f:
    inp = json.load(f)
spec = importlib.util.spec_from_file_location("entry", job + "/" + inp["__entry__"])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
res = mod.main(inp["args"])
with open(out + "/result.json", "w") as f:
    json.dump(res, f)
'''

MAX_RESULT_BYTES = 8 * 1024 * 1024
DEFAULT_IMAGE = os.getenv('VUPPIA_SANDBOX_IMAGE', 'vuppia-sandbox:3')


class SandboxError(RuntimeError):
    pass


def docker_available() -> bool:
    if shutil.which('docker') is None:
        return False
    try:
        cp = subprocess.run(['docker', 'image', 'inspect', DEFAULT_IMAGE, '--format', '{{.Id}}'],
                            capture_output=True, text=True, timeout=15)
        return cp.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def image_id() -> str | None:
    try:
        cp = subprocess.run(['docker', 'image', 'inspect', DEFAULT_IMAGE, '--format', '{{.Id}}'],
                            capture_output=True, text=True, timeout=15)
        return cp.stdout.strip() if cp.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _prepare(files: dict[str, bytes], entry: str, args: dict) -> tuple[Path, Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory(prefix='vuppia_sbx_')
    root = Path(tmp.name)
    job, out = root / 'job', root / 'out'
    job.mkdir(); out.mkdir()
    for name, data in files.items():
        if '/' in name or name.startswith('.'):
            raise SandboxError('nombre de archivo inválido: ' + name)
        (job / name).write_bytes(data)
    (job / '_runner.py').write_text(RUNNER)
    (job / 'input.json').write_text(json.dumps({'__entry__': entry, 'args': args}))
    os.chmod(root, 0o755); os.chmod(job, 0o755); os.chmod(out, 0o777)
    for p in job.iterdir():
        os.chmod(p, 0o644)
    return job, out, tmp


def _collect(out: Path) -> dict:
    f = out / 'result.json'
    if not f.exists():
        raise SandboxError('el código no produjo result.json')
    if f.stat().st_size > MAX_RESULT_BYTES:
        raise SandboxError('resultado demasiado grande')
    try:
        return json.loads(f.read_text())
    except ValueError as e:
        raise SandboxError('result.json inválido') from e


PINNED_DIGEST = os.getenv('VUPPIA_SANDBOX_DIGEST', '').strip()


def run(files: dict[str, bytes], entry: str, args: dict, *, untrusted: bool, timeout: float = 120,
        memory: str = '768m', cpus: str = '1.0') -> dict:
    if untrusted:
        # El código no confiable SOLO corre en Docker endurecido con un digest de imagen VÁLIDO y
        # FIJADO. Sin digest no se ejecuta (3.2: el digest es obligatorio, no opcional).
        if not PINNED_DIGEST:
            raise SandboxError('VUPPIA_SANDBOX_DIGEST no está configurado: el código no confiable NO se '
                               'ejecuta sin un digest de imagen fijado. Fijá el digest de vuppia-sandbox.')
        if not PINNED_DIGEST.startswith('sha256:') or len(PINNED_DIGEST) < 71:
            raise SandboxError('VUPPIA_SANDBOX_DIGEST no es un digest sha256 válido.')
        if not docker_available():
            raise SandboxError('Docker con la imagen fijada no está disponible: el código no confiable '
                               'NO se ejecuta sin aislamiento fuerte (no hay fallback débil).')
        img = image_id() or ''
        if img != PINNED_DIGEST:
            raise SandboxError(f'La imagen del sandbox ({img[:20]}) no coincide con el digest fijado '
                               f'VUPPIA_SANDBOX_DIGEST ({PINNED_DIGEST[:20]}): no se ejecuta código no confiable.')
        return _run_docker(files, entry, args, timeout=timeout, memory=memory, cpus=cpus)
    # Código CONFIABLE del núcleo (protocolos integrados): Docker si está, local si no.
    if docker_available():
        return _run_docker(files, entry, args, timeout=timeout, memory=memory, cpus=cpus)
    return _run_local(files, entry, args, timeout=timeout)


def _run_docker(files, entry, args, *, timeout, memory, cpus) -> dict:
    job, out, tmp = _prepare(files, entry, args)
    name = 'vuppia_' + uuid.uuid4().hex[:12]
    cmd = ['docker', 'run', '--rm', '--name', name, '--network', 'none', '--read-only',
           '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '64',
           '--memory', memory, '--memory-swap', memory, '--cpus', cpus, '--user', '65534:65534',
           '--ulimit', 'nofile=64:64', '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16m',
           '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'OPENBLAS_NUM_THREADS=1',
           '-v', f'{job}:/job:ro', '-v', f'{out}:/out:rw', DEFAULT_IMAGE,
           'python', '-I', '-B', '/job/_runner.py', '/job', '/out']
    t0 = time.time()
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(['docker', 'kill', name], capture_output=True)
        tmp.cleanup()
        raise SandboxError(f'tiempo máximo ({timeout}s) superado; contenedor eliminado')
    try:
        if cp.returncode != 0:
            raise SandboxError(f'código salió con {cp.returncode}: {cp.stderr[-600:]}')
        return {'result': _collect(out), 'runtime_s': round(time.time() - t0, 3), 'backend': 'docker',
                'image': image_id(), 'stderr_tail': cp.stderr[-300:]}
    finally:
        tmp.cleanup()


def _limits():
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024 ** 3, 2 * 1024 ** 3))
    resource.setrlimit(resource.RLIMIT_CPU, (300, 300))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RESULT_BYTES * 2, MAX_RESULT_BYTES * 2))


def _run_local(files, entry, args, *, timeout) -> dict:
    job, out, tmp = _prepare(files, entry, args)
    import sys
    base = [sys.executable, '-I', '-B', str(job / '_runner.py'), str(job), str(out)]
    netless = shutil.which('unshare') and os.geteuid() == 0
    cmd = (['unshare', '-n'] + base) if netless else base
    env = {'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1', 'OPENBLAS_NUM_THREADS': '1'}
    t0 = time.time()
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=job, env=env,
                            preexec_fn=_limits)
        if cp.returncode != 0:
            raise SandboxError(f'código salió con {cp.returncode}: {cp.stderr[-600:]}')
        return {'result': _collect(out), 'runtime_s': round(time.time() - t0, 3),
                'backend': 'local-unshare' if netless else 'local-weak', 'image': None,
                'stderr_tail': cp.stderr[-300:]}
    except subprocess.TimeoutExpired:
        raise SandboxError(f'tiempo máximo ({timeout}s) superado')
    finally:
        tmp.cleanup()
