#!/usr/bin/env python3
"""WAV Merger engine — WAV/M4B merge, mastering and chapter export.

Extracted from WAV Merger Pro v3.0.0 for the full-cast audiobook pipeline.
100% Python stdlib; FFmpeg/ffprobe must be on PATH.
"""

import sys, os, wave, audioop, threading, time, struct, math, json, shutil, subprocess, re, tempfile
APP_NAME    = "WAV Merger Pro"
APP_VERSION = "3.0.0"
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".wavmerger_settings.json")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_FFMPEG = os.path.join(BASE_DIR, "tools", "ffmpeg", "bin", "ffmpeg")


# ══════════════════════════════════════════════════════════════════
#  AUDIO ENGINE
# ══════════════════════════════════════════════════════════════════

def find_ffmpeg():
    candidates = [LOCAL_FFMPEG, shutil.which("ffmpeg")]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def ffmpeg_cmd(*args):
    binary = find_ffmpeg()
    if not binary:
        raise FileNotFoundError("FFmpeg not found")
    return [binary, *args]

def secs_to_hms(s):
    h = int(s // 3600); m = int((s % 3600) // 60); s = s % 60
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m}:{s:05.2f}"

def _read_wav_header(path):
    # Handles WAVE_FORMAT_EXTENSIBLE (0xFFFE) and RF64 (files > 4 GB)
    with open(path, 'rb') as f:
        riff = f.read(12)
        is_rf64 = riff[:4] == b'RF64'
        if not is_rf64 and riff[:4] != b'RIFF':
            raise ValueError("Not a valid WAV file")
        if riff[8:12] != b'WAVE':
            raise ValueError("Not a valid WAV file")

        # RF64: read ds64 chunk right after the 12-byte file header
        # to get the real 64-bit data size (RIFF size fields are 0xFFFFFFFF placeholders)
        real_data_size_64 = None
        if is_rf64:
            ds64_hdr = f.read(8)
            if len(ds64_hdr) == 8 and ds64_hdr[:4] == b'ds64':
                ds64_size = struct.unpack_from('<I', ds64_hdr, 4)[0]
                ds64_body = f.read(ds64_size)
                # ds64 body: riff_size_64(8) + data_size_64(8) + sample_count_64(8) + ...
                if len(ds64_body) >= 16:
                    real_data_size_64 = struct.unpack_from('<Q', ds64_body, 8)[0]
                # file position is now correctly right after ds64 — no extra seeking needed

        ch = sw = fr = nf = data_size = 0
        while True:
            hdr = f.read(8)
            if len(hdr) < 8: break
            tag, size = hdr[:4], struct.unpack_from('<I', hdr, 4)[0]
            if tag == b'fmt ':
                fmt = f.read(size)
                fmt_tag = struct.unpack_from('<H', fmt, 0)[0]
                if fmt_tag not in (1, 3, 65534):
                    raise ValueError(f"Unsupported WAV format tag: {fmt_tag}")
                ch = struct.unpack_from('<H', fmt, 2)[0]
                fr = struct.unpack_from('<I', fmt, 4)[0]
                sw = struct.unpack_from('<H', fmt, 14)[0] // 8
            elif tag == b'data':
                # RF64 uses 0xFFFFFFFF as placeholder; use real size from ds64
                if is_rf64 and size == 0xFFFFFFFF and real_data_size_64 is not None:
                    data_size = real_data_size_64
                else:
                    data_size = size
                break
            else:
                f.seek(size + (size & 1), 1)
        if not ch or not fr or not sw:
            raise ValueError("Could not parse WAV fmt chunk")
        nf = data_size // (ch * sw) if sw and ch else 0
        return ch, sw, fr, nf

def get_wav_info(path):
    try:
        try:
            with wave.open(path, 'rb') as w:
                ch, sw, fr, nf = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        except Exception:
            ch, sw, fr, nf = _read_wav_header(path)
        dur = nf / fr if fr else 0
        return dict(path=path, name=os.path.basename(path), channels=ch, sampwidth=sw,
                    framerate=fr, n_frames=nf, duration=dur, dur_str=secs_to_hms(dur),
                    size=os.path.getsize(path), error=None)
    except Exception as e:
        return dict(path=path, name=os.path.basename(path), error=str(e))

def rms_db(data, sw):
    rms = audioop.rms(data, sw)
    if rms == 0: return -96.0
    max_val = (1 << (sw * 8 - 1)) - 1
    return 20 * math.log10(rms / max_val)

def peak_db(data, sw):
    peak = audioop.max(data, sw)
    if peak == 0: return -96.0
    max_val = (1 << (sw * 8 - 1)) - 1
    return 20 * math.log10(peak / max_val)


# ── FFmpeg stderr parsers ────────────────────────────────────────────────
_RE_TIME  = re.compile(r'time=(-?)(\d+):(\d+):(\d+\.\d+)')
_RE_SPEED = re.compile(r'speed=\s*([\d.]+)x')
_RE_SIZE  = re.compile(r'size=\s*(\d+)kB')

PCM_CODEC_BY_WIDTH = {
    1: "pcm_u8",
    2: "pcm_s16le",
    3: "pcm_s24le",
    4: "pcm_s32le",
}

def _target_codec(options):
    sw = int(options.get('tgt_sampwidth', 2))
    if sw not in PCM_CODEC_BY_WIDTH:
        raise ValueError(f"Unsupported target sample width: {sw} bytes")
    return PCM_CODEC_BY_WIDTH[sw]

def _channel_layout(channels):
    return "mono" if int(channels) == 1 else "stereo"

def _temp_path_near(final_path, suffix=None):
    final_path = os.path.abspath(final_path)
    folder = os.path.dirname(final_path) or os.getcwd()
    base, ext = os.path.splitext(os.path.basename(final_path))
    fd, path = tempfile.mkstemp(
        prefix=f".{base}.",
        suffix=suffix or f".tmp{ext or '.wav'}",
        dir=folder,
    )
    os.close(fd)
    return path

def _atomic_replace(src, dst):
    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    os.replace(src, dst)

def _cleanup_paths(*paths):
    for path in paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

def _concat_escape(path):
    return os.path.abspath(path).replace("\\", "/").replace("'", "'\\''")

def _build_atempo_chain(speed):
    speed = float(speed or 1.0)
    if abs(speed - 1.0) <= 0.01:
        return []
    chain = []
    r = speed
    while r < 0.5:
        chain.append('atempo=0.5')
        r /= 0.5
    while r > 2.0:
        chain.append('atempo=2.0')
        r /= 2.0
    chain.append(f'atempo={r:.4f}')
    return chain

def _base_filters(options, total_dur, include_master_prep=True):
    filters = []
    filters.extend(_build_atempo_chain(options.get('speed', 1.0)))

    trim_db = int(options.get('trim_threshold_db', -55))
    if options.get('trim_silence', False):
        filters.append(
            "silenceremove="
            f"start_periods=1:start_duration=0.10:start_threshold={trim_db}dB:"
            f"stop_periods=1:stop_duration=0.10:stop_threshold={trim_db}dB"
        )

    if include_master_prep and options.get('acx_master', False):
        filters.extend([
            'highpass=f=80',
            'acompressor=threshold=-15dB:ratio=4:makeup=3',
        ])

    gain = float(options.get('gain_db', 0.0) or 0.0)
    if abs(gain) > 0.01:
        filters.append(f"volume={gain:.4f}dB")

    speed = float(options.get('speed', 1.0) or 1.0)
    duration_after_speed = total_dur / speed if speed > 0 else total_dur
    fade_in = int(options.get('fade_in_ms', 0) or 0) / 1000
    fade_out = int(options.get('fade_out_ms', 0) or 0) / 1000
    if fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0 and duration_after_speed > 0:
        start = max(0.0, duration_after_speed - fade_out)
        filters.append(f"afade=t=out:st={start:.3f}:d={fade_out:.3f}")

    return filters

def _output_args(options, output_path):
    return [
        '-ar', str(int(options.get('tgt_framerate', 44100))),
        '-ac', str(int(options.get('tgt_channels', 2))),
        '-c:a', _target_codec(options),
        '-rf64', 'auto',
        output_path,
    ]

def _preflight_merge(file_infos, output_path, options, log_cb):
    if not find_ffmpeg():
        raise FileNotFoundError("FFmpeg not found")
    if not file_infos:
        raise ValueError("No files to merge")

    out_dir = os.path.dirname(os.path.abspath(output_path)) or os.getcwd()
    os.makedirs(out_dir, exist_ok=True)
    if not os.access(out_dir, os.W_OK):
        raise PermissionError(f"Output folder is not writable: {out_dir}")

    ext = os.path.splitext(output_path)[1].lower()
    if ext and ext != ".wav":
        log_cb(f"Output extension is {ext}; WAV output is recommended for the merge stage.", 'warning')

    target = (
        int(options.get('tgt_channels', 2)),
        int(options.get('tgt_sampwidth', 2)),
        int(options.get('tgt_framerate', 44100)),
    )
    speed = float(options.get('speed', 1.0) or 1.0)
    if speed <= 0:
        raise ValueError("Speed must be greater than 0.")

    for info in file_infos:
        path = info.get('path')
        if not path or not os.path.exists(path):
            raise FileNotFoundError(f"Input file not found: {path}")
        if not os.access(path, os.R_OK):
            raise PermissionError(f"Input file is not readable: {path}")
        if not options.get('auto_convert', True):
            actual = (int(info.get('channels', 0)), int(info.get('sampwidth', 0)), int(info.get('framerate', 0)))
            if actual != target:
                raise ValueError(f"{info.get('name', path)} does not match target format. Enable Auto-Convert.")

    if options.get('silence_ms', 0) and options.get('crossfade_ms', 0):
        log_cb("Crossfade enabled; silence gaps will be skipped.", 'warning')
    xfade_s = int(options.get('crossfade_ms', 0) or 0) / 1000
    if xfade_s > 0 and len(file_infos) > 1:
        shortest = min(max(0.0, f.get('duration', 0.0)) for f in file_infos)
        if shortest and xfade_s >= shortest:
            raise ValueError(f"Crossfade ({xfade_s:.2f}s) must be shorter than the shortest track ({shortest:.2f}s).")

    total_dur = sum(f.get('duration', 0) for f in file_infos)
    estimated_bytes = total_dur * int(options.get('tgt_framerate', 44100)) * int(options.get('tgt_channels', 2)) * int(options.get('tgt_sampwidth', 2))
    free = shutil.disk_usage(out_dir).free
    if estimated_bytes and free < estimated_bytes * 1.25:
        log_cb(f"Low disk space: estimated WAV is {estimated_bytes / (1024**3):.2f} GB, free is {free / (1024**3):.2f} GB.", 'warning')

def _parse_ffmpeg_line(line):
    """Return (elapsed_s, speed, size_mb) from an FFmpeg progress line, or None."""
    m = _RE_TIME.search(line)
    if not m or m.group(1) == '-':
        return None
    h, mn, s = int(m.group(2)), int(m.group(3)), float(m.group(4))
    elapsed = h * 3600 + mn * 60 + s
    sm = _RE_SPEED.search(line); speed  = float(sm.group(1)) if sm else None
    km = _RE_SIZE.search(line);  size_mb = int(km.group(1)) / 1024 if km else None
    return elapsed, speed, size_mb

def _run_ffmpeg_live(cmd, total_dur, progress_cb, log_cb,
                     pct_start=20, pct_end=98, cancel_event=None):
    """
    Run an FFmpeg command and stream structured progress in real time.
    progress_cb(pct, elapsed, total, speed, size_mb) — all args except pct optional.
    Returns (returncode, stderr_tail_lines).
    """
    run_cmd = [cmd[0], '-hide_banner', '-nostats', '-loglevel', 'error', '-progress', 'pipe:1'] + cmd[1:]
    stderr_lines = []
    progress_state = {}
    proc = subprocess.Popen(
        run_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace',
    )
    try:
        while True:
            if cancel_event and cancel_event.is_set():
                proc.kill()
                proc.wait()
                return -1, stderr_lines
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            line = line.strip()
            if not line or '=' not in line:
                continue
            key, value = line.split('=', 1)
            progress_state[key] = value

            elapsed = None
            if progress_state.get('out_time_us'):
                try: elapsed = int(progress_state['out_time_us']) / 1_000_000
                except ValueError: elapsed = None
            elif progress_state.get('out_time_ms'):
                try: elapsed = int(progress_state['out_time_ms']) / 1_000_000
                except ValueError: elapsed = None
            elif progress_state.get('out_time'):
                parsed = _parse_ffmpeg_line(f"time={progress_state['out_time']}")
                elapsed = parsed[0] if parsed else None

            speed = None
            if progress_state.get('speed'):
                try: speed = float(progress_state['speed'].replace('x', ''))
                except ValueError: speed = None

            size_mb = None
            if progress_state.get('total_size'):
                try: size_mb = int(progress_state['total_size']) / (1024 * 1024)
                except ValueError: size_mb = None

            if elapsed is not None and total_dur > 0:
                pct = pct_start + min(pct_end - pct_start,
                                      (elapsed / total_dur) * (pct_end - pct_start))
                progress_cb(pct, elapsed, total_dur, speed, size_mb)

            if key == 'progress' and value == 'end':
                break
    finally:
        _, stderr = proc.communicate()
        stderr_lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        for line in stderr_lines:
            if any(k.lower() in line.lower() for k in ('error', 'invalid', 'no such', 'failed')):
                log_cb(f"  ffmpeg: {line}", 'warning')
    return proc.returncode, stderr_lines

def _run_ffmpeg_capture(cmd):
    run_cmd = [cmd[0], '-hide_banner', '-nostats'] + cmd[1:]
    return subprocess.run(
        run_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace',
    )

def _run_ffmpeg_capture_live(cmd, total_dur, progress_cb, log_cb,
                             pct_start=0, pct_end=100, cancel_event=None):
    """
    Run an FFmpeg command that needs captured stderr while still streaming
    -progress updates from stdout.
    """
    run_cmd = [cmd[0], '-hide_banner', '-nostats', '-progress', 'pipe:1'] + cmd[1:]
    progress_state = {}
    stderr_chunks = []
    proc = subprocess.Popen(
        run_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace',
    )

    def _read_stderr():
        for chunk in proc.stderr:
            stderr_chunks.append(chunk)

    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    stderr_thread.start()
    try:
        while True:
            if cancel_event and cancel_event.is_set():
                proc.kill()
                proc.wait()
                return -1, ''.join(stderr_chunks).splitlines()
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            line = line.strip()
            if not line or '=' not in line:
                continue
            key, value = line.split('=', 1)
            progress_state[key] = value

            elapsed = None
            if progress_state.get('out_time_us'):
                try: elapsed = int(progress_state['out_time_us']) / 1_000_000
                except ValueError: elapsed = None
            elif progress_state.get('out_time_ms'):
                try: elapsed = int(progress_state['out_time_ms']) / 1_000_000
                except ValueError: elapsed = None
            elif progress_state.get('out_time'):
                parsed = _parse_ffmpeg_line(f"time={progress_state['out_time']}")
                elapsed = parsed[0] if parsed else None

            speed = None
            if progress_state.get('speed'):
                try: speed = float(progress_state['speed'].replace('x', ''))
                except ValueError: speed = None

            size_mb = None
            if progress_state.get('total_size'):
                try: size_mb = int(progress_state['total_size']) / (1024 * 1024)
                except ValueError: size_mb = None

            if elapsed is not None and total_dur > 0:
                pct = pct_start + min(pct_end - pct_start,
                                      (elapsed / total_dur) * (pct_end - pct_start))
                progress_cb(pct, elapsed, total_dur, speed, size_mb)

            if key == 'progress' and value == 'end':
                break
    finally:
        proc.wait()
        stderr_thread.join(timeout=1)

    stderr_text = ''.join(stderr_chunks)
    stderr_lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
    for line in stderr_lines:
        if any(k.lower() in line.lower() for k in ('error', 'invalid', 'no such', 'failed')):
            log_cb(f"  ffmpeg: {line}", 'warning')
    return proc.returncode, stderr_lines

def _parse_loudnorm_json(stderr_text):
    matches = re.findall(r'\{[\s\S]*?\}', stderr_text)
    if not matches:
        raise ValueError("Could not parse loudnorm analysis output")
    data = json.loads(matches[-1])
    required = ('input_i', 'input_tp', 'input_lra', 'input_thresh', 'target_offset')
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"Missing loudnorm values: {', '.join(missing)}")
    return data

def _run_loudnorm_two_pass(input_path, output_temp_path, options, total_dur, progress_cb, log_cb, status_cb=None, cancel_event=None):
    target_i = -20 if options.get('acx_master', False) else -23
    target_lra = 11
    target_tp = -3 if options.get('acx_master', False) else -1.5

    if status_cb:
        status_cb("Analyzing loudness (pass 1 of 2)...")
    analysis_filter = f"loudnorm=I={target_i}:LRA={target_lra}:TP={target_tp}:print_format=json"
    analysis_cmd = ffmpeg_cmd(
        '-y', '-i', input_path,
        '-af', analysis_filter,
        '-f', 'null', '-',
    )
    progress_cb(70)
    rc, err_lines = _run_ffmpeg_capture_live(
        analysis_cmd, total_dur, progress_cb, log_cb,
        pct_start=70, pct_end=82, cancel_event=cancel_event,
    )
    if rc != 0:
        for line in err_lines[-12:]:
            log_cb(f"  {line}", 'muted')
        return rc, err_lines
    measured = _parse_loudnorm_json("\n".join(err_lines))

    if status_cb:
        status_cb("Applying loudness correction (pass 2 of 2)...")
    log_cb("Loudnorm: two-pass measured normalization", 'muted')
    final_filter = (
        f"loudnorm=I={target_i}:LRA={target_lra}:TP={target_tp}:"
        f"measured_I={measured['input_i']}:"
        f"measured_TP={measured['input_tp']}:"
        f"measured_LRA={measured['input_lra']}:"
        f"measured_thresh={measured['input_thresh']}:"
        f"offset={measured['target_offset']}:"
        "linear=true:print_format=summary"
    )
    cmd = ffmpeg_cmd(
        '-y', '-i', input_path,
        '-af', final_filter,
    ) + _output_args(options, output_temp_path)
    return _run_ffmpeg_live(cmd, total_dur, progress_cb, log_cb, pct_start=82, pct_end=98, cancel_event=cancel_event)

def _create_silence_wav(duration_s, options, folder):
    path = _temp_path_near(os.path.join(folder, "silence.wav"), suffix=".tmp.wav")
    cmd = ffmpeg_cmd(
        '-hide_banner', '-nostats', '-loglevel', 'error', '-y',
        '-f', 'lavfi',
        '-i', f"anullsrc=r={int(options.get('tgt_framerate', 44100))}:cl={_channel_layout(options.get('tgt_channels', 2))}",
        '-t', f"{duration_s:.3f}",
    ) + _output_args(options, path)
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return path

def _build_concat_command(file_infos, concat_list_path, output_path, options, filters):
    cmd = ffmpeg_cmd('-y', '-f', 'concat', '-safe', '0', '-i', concat_list_path)
    if filters:
        cmd += ['-af', ','.join(filters)]
    cmd += _output_args(options, output_path)
    return cmd

def _build_crossfade_command(file_infos, output_path, options, filters):
    xfade_s = int(options.get('crossfade_ms', 0) or 0) / 1000
    cmd = ffmpeg_cmd('-y')
    for info in file_infos:
        cmd += ['-i', info['path']]

    if len(file_infos) < 2 or xfade_s <= 0:
        raise ValueError("Crossfade requires at least 2 files and a positive duration.")

    rate = int(options.get('tgt_framerate', 44100))
    layout = _channel_layout(options.get('tgt_channels', 2))
    parts = []
    for index in range(len(file_infos)):
        parts.append(f"[{index}:a]aresample={rate},aformat=channel_layouts={layout}[a{index}]")

    previous = '[a0]'
    for index in range(1, len(file_infos)):
        out_label = f"[xf{index}]"
        parts.append(f"{previous}[a{index}]acrossfade=d={xfade_s:.3f}:c1=tri:c2=tri{out_label}")
        previous = out_label

    final_label = previous
    if filters:
        parts.append(f"{final_label}{','.join(filters)}[out]")
        final_label = '[out]'

    cmd += ['-filter_complex', ';'.join(parts), '-map', final_label]
    cmd += _output_args(options, output_path)
    return cmd


def do_merge(file_infos, output_path, options, progress_cb, log_cb,
             status_cb=None, cancel_event=None):
    """
    FFmpeg stream engine: merges files without loading PCM into Python memory.
    Python never loads audio PCM into memory — FFmpeg reads directly from disk.

    progress_cb(pct, elapsed=None, total=None, speed=None, size_mb=None)
    status_cb(stage_text)   — optional, updates a stage label
    cancel_event            — optional threading.Event; set it to abort
    """
    def _status(txt):
        if status_cb: status_cb(txt)

    concat_list_path = None
    silence_path = None
    pre_master_path = None
    temp_output_path = None
    try:
        options = dict(options)
        if options.get('silence_ms', 0) and options.get('crossfade_ms', 0):
            log_cb("Crossfade enabled; silence gaps will be skipped.", 'warning')
            options['silence_ms'] = 0
        n         = len(file_infos)
        out_dir   = os.path.dirname(os.path.abspath(output_path))
        total_dur = sum(f['duration'] for f in file_infos)
        gap_s     = int(options.get('silence_ms', 0) or 0) / 1000
        xfade_s   = int(options.get('crossfade_ms', 0) or 0) / 1000
        effective_dur = max(0.1, total_dur + (gap_s * max(0, n - 1)) - (xfade_s * max(0, n - 1)))
        _preflight_merge(file_infos, output_path, options, log_cb)

        os.makedirs(out_dir, exist_ok=True)
        concat_fd, concat_list_path = tempfile.mkstemp(prefix="concat_", suffix=".txt", dir=out_dir)
        os.close(concat_fd)

        log_cb(f"Engine  : FFmpeg stream  (single engine, disk-to-disk)", 'success')
        log_cb(f"Files   : {n}  |  Total duration: ~{secs_to_hms(total_dur)}")
        log_cb(f"Output  : {output_path}")
        log_cb(f"Format  : {options.get('tgt_framerate', 44100)} Hz  |  {options.get('tgt_channels', 2)} ch  |  {options.get('tgt_sampwidth', 2) * 8}-bit")
        log_cb("─" * 52)

        # ── Write concat list ────────────────────────────────────────
        _status("Writing file list…")
        progress_cb(5)
        with open(concat_list_path, 'w', encoding='utf-8') as cl:
            if gap_s > 0:
                silence_path = _create_silence_wav(gap_s, options, out_dir)
                log_cb(f"Gap     : {gap_s:.2f}s between tracks", 'muted')
            for index, info in enumerate(file_infos):
                cl.write(f"file '{_concat_escape(info['path'])}'\n")
                log_cb(f"  + {info['name']}", 'muted')
                if silence_path and index < n - 1:
                    cl.write(f"file '{_concat_escape(silence_path)}'\n")

        progress_cb(10)
        if cancel_event and cancel_event.is_set():
            return False, "Cancelled", []

        # ── Build audio filter chain ─────────────────────────────────
        speed    = float(options.get('speed', 1.0) or 1.0)
        output_dur = max(0.1, effective_dur / speed)
        do_speed = abs(speed - 1.0) > 0.01
        do_acx   = options.get('acx_master', False)
        do_loudnorm = do_acx or options.get('normalize_master', False)
        filters = _base_filters(options, effective_dur, include_master_prep=True)

        if do_speed:
            log_cb(f"Speed    : {speed:.2f}x  (pitch-preserving atempo chain)", 'muted')
        if do_acx:
            log_cb("ACX      : highpass → compressor → two-pass loudnorm -20 LUFS", 'muted')
        elif options.get('normalize_master', False):
            log_cb("Normalize: two-pass loudnorm -23 LUFS", 'muted')
        if options.get('trim_silence', False):
            log_cb(f"Trim     : leading/trailing silence at {options.get('trim_threshold_db', -55)} dB", 'muted')
        if options.get('fade_in_ms', 0) or options.get('fade_out_ms', 0):
            log_cb(f"Fades    : in {options.get('fade_in_ms', 0)}ms  |  out {options.get('fade_out_ms', 0)}ms", 'muted')

        # ── Resolve final output path ────────────────────────────────
        suffix = ''
        if do_speed: suffix += f'_{speed:.2f}x'
        if do_acx:   suffix += '_ACX_Mastered'
        elif options.get('normalize_master', False): suffix += '_Normalized'
        if suffix:
            base, ext = os.path.splitext(output_path)
            final_output_path = f"{base}{suffix}{ext}"
        else:
            final_output_path = output_path

        log_cb(f"Final    : {final_output_path}", 'muted')
        log_cb("─" * 52)

        temp_output_path = _temp_path_near(final_output_path)
        working_output_path = _temp_path_near(final_output_path) if do_loudnorm else temp_output_path
        if do_loudnorm:
            pre_master_path = working_output_path

        stage = "Merging"
        if do_acx:   stage += " + ACX Master"
        if do_speed: stage += f" ({speed:.2f}x speed)"
        if options.get('crossfade_ms', 0):
            stage += f" + {options.get('crossfade_ms')}ms crossfade"
        _status(f"{stage}…")
        log_cb(f"Running FFmpeg ({stage})…", 'muted')

        if options.get('crossfade_ms', 0):
            cmd = _build_crossfade_command(file_infos, working_output_path, options, filters)
        else:
            cmd = _build_concat_command(file_infos, concat_list_path, working_output_path, options, filters)

        rc, err_lines = _run_ffmpeg_live(
            cmd, output_dur, progress_cb, log_cb,
            pct_start=12, pct_end=70 if do_loudnorm else 98, cancel_event=cancel_event,
        )

        if cancel_event and cancel_event.is_set():
            log_cb("✗ Merge cancelled.", 'warning')
            return False, "Cancelled", []

        if rc != 0:
            log_cb(f"✗ FFmpeg exited with code {rc}:", 'error')
            for line in err_lines[-14:]:
                log_cb(f"  {line}", 'muted')
            return False, "\n".join(err_lines), []

        if do_loudnorm:
            rc, err_lines = _run_loudnorm_two_pass(
                pre_master_path,
                temp_output_path,
                options,
                output_dur,
                progress_cb,
                log_cb,
                status_cb=status_cb,
                cancel_event=cancel_event,
            )
            if cancel_event and cancel_event.is_set():
                log_cb("✗ Merge cancelled.", 'warning')
                return False, "Cancelled", []
            if rc != 0:
                log_cb(f"✗ Loudnorm pass failed with code {rc}:", 'error')
                for line in err_lines[-14:]:
                    log_cb(f"  {line}", 'muted')
                return False, "\n".join(err_lines), []

        _status("Finalizing output...")
        _atomic_replace(temp_output_path, final_output_path)

        file_mb = os.path.getsize(final_output_path) / (1024 * 1024)
        progress_cb(100, output_dur, output_dur, None, file_mb)
        _status("✓  Merge complete!")
        log_cb(f"\n✓ FFmpeg stream merge complete!", 'success')
        log_cb(f"  Duration : ~{secs_to_hms(output_dur)}", 'success')
        log_cb(f"  File size: {file_mb:.2f} MB", 'success')
        log_cb(f"  Saved to : {final_output_path}", 'success')

        track_stats = [dict(name=f['name'], duration=f['duration'],
                            peak_db=0.0, rms_db=0.0) for f in file_infos]
        return True, final_output_path, track_stats

    except FileNotFoundError:
        log_cb("✗ FFmpeg not found — install from https://ffmpeg.org/download.html", 'error')
        return False, "FFmpeg not found", []
    except Exception as e:
        import traceback
        log_cb(f"\n✗ ERROR: {e}", 'error')
        log_cb(traceback.format_exc(), 'muted')
        return False, str(e), []
    finally:
        _cleanup_paths(concat_list_path, silence_path, pre_master_path, temp_output_path)


# ══════════════════════════════════════════════════════════════════
#  M4B EXPORT ENGINE
# ══════════════════════════════════════════════════════════════════

def _chapter_timeline(file_infos, merge_options=None):
    opts = merge_options or {}
    speed = max(0.001, float(opts.get('speed', 1.0) or 1.0))
    xfade_s = int(opts.get('crossfade_ms', 0) or 0) / 1000
    gap_s = 0.0 if xfade_s > 0 else int(opts.get('silence_ms', 0) or 0) / 1000
    cursor = 0.0
    starts = []

    for index, info in enumerate(file_infos):
        starts.append(cursor / speed)
        dur = max(0.0, float(info.get('duration', 0.0)))
        if index < len(file_infos) - 1:
            cursor += max(0.0, dur - xfade_s) + gap_s
        else:
            cursor += dur

    total_s = max(0.001, cursor / speed)
    # A TTS run may split one story chapter into files such as
    # ``Chapter 01.wav``, ``Chapter 01_2.wav``, etc.  Those are continuation
    # parts, not separate audiobook chapters.  Group only contiguous files
    # that use this explicit chapter-part naming convention; arbitrary input
    # filenames continue to produce one marker per file.
    part_re = re.compile(r"^(.*\bChapter\s+\d+)(?:_\d+)?$", re.IGNORECASE)
    chapters = []
    for index, info in enumerate(file_infos):
        stem = os.path.splitext(info['name'])[0]
        match = part_re.match(stem)
        key = match.group(1) if match else None
        title = key or stem
        start_ms = int(round(starts[index] * 1000))
        end_s = starts[index + 1] if index < len(starts) - 1 else total_s
        end_ms = max(start_ms + 1, int(round(end_s * 1000)))

        if key and chapters and chapters[-1][3] == key:
            old_title, old_start, _old_end, _old_key = chapters[-1]
            chapters[-1] = (old_title, old_start, end_ms, key)
        else:
            chapters.append((title, start_ms, end_ms, key))

    chapters = [(title, start_ms, end_ms)
                for title, start_ms, end_ms, _key in chapters]
    return chapters, total_s

def export_m4b(wav_path, file_infos, bitrate, log_cb,
               progress_cb=None, status_cb=None, cancel_event=None,
               merge_options=None):
    """Encode a WAV file to a chaptered M4B using FFmpeg."""
    def _prog(pct, elapsed=None, total=None, speed=None, size_mb=None):
        if progress_cb: progress_cb(pct, elapsed, total, speed, size_mb)
    def _status(txt):
        if status_cb: status_cb(txt)

    base      = os.path.splitext(wav_path)[0]
    m4b_path  = base + ".m4b"
    meta_fd, meta_path = tempfile.mkstemp(prefix=os.path.basename(base) + "_chapters_", suffix=".txt", dir=os.path.dirname(os.path.abspath(wav_path)) or None)
    os.close(meta_fd)
    temp_m4b_path = _temp_path_near(m4b_path, suffix=".tmp.m4b")

    log_cb("─" * 52)
    log_cb("Exporting chaptered M4B…", 'success')
    _status("Writing chapter markers…")
    _prog(2)

    # Build ffmetadata chapter file
    lines     = [";FFMETADATA1\n"]
    chapters, total_dur = _chapter_timeline(file_infos, merge_options)
    for title, start_ms, end_ms in chapters:
        lines += ["[CHAPTER]\n", "TIMEBASE=1/1000\n",
                  f"START={start_ms}\n", f"END={end_ms}\n",
                  f"title={title}\n", "\n"]
        log_cb(f"  Ch {title[:40]}  @{secs_to_hms(start_ms/1000)}", 'muted')

    with open(meta_path, 'w', encoding='utf-8') as mf:
        mf.writelines(lines)

    log_cb(f"Chapters : {len(chapters)} markers written from {len(file_infos)} WAV tracks", 'muted')
    log_cb(f"Bitrate  : {bitrate}kbps AAC", 'muted')
    log_cb(f"Output   : {m4b_path}", 'muted')
    _status(f"Encoding M4B at {bitrate}kbps…")
    _prog(5)

    try:
        cmd = ffmpeg_cmd('-y',
               '-i', wav_path, '-i', meta_path,
               '-map_metadata', '1',
               '-c:a', 'aac', '-b:a', f'{bitrate}k',
               '-movflags', '+faststart',
               temp_m4b_path)

        rc, err_lines = _run_ffmpeg_live(
            cmd, total_dur, _prog, log_cb,
            pct_start=5, pct_end=98, cancel_event=cancel_event,
        )

        if cancel_event and cancel_event.is_set():
            log_cb("✗ M4B export cancelled.", 'warning')
            return False, None

        if rc != 0:
            log_cb(f"✗ FFmpeg M4B error (exit {rc}):", 'error')
            for line in err_lines[-8:]:
                log_cb(f"  {line}", 'muted')
            return False, None

        _atomic_replace(temp_m4b_path, m4b_path)
        size_mb = os.path.getsize(m4b_path) / (1024 * 1024)
        _prog(100)
        _status("✓  M4B export complete!")
        log_cb(f"✓ M4B export complete!  {size_mb:.1f} MB", 'success')
        log_cb(f"  Saved to: {m4b_path}", 'success')
        return True, m4b_path

    except FileNotFoundError:
        log_cb("✗ FFmpeg not found — install from https://ffmpeg.org/download.html", 'error')
        return False, None
    except Exception as e:
        log_cb(f"✗ M4B error: {e}", 'error')
        return False, None
    finally:
        _cleanup_paths(meta_path, temp_m4b_path)
