#!/usr/bin/env python3
"""Compress detected silences in M4B audiobooks while retiming embedded chapters.

Requires Python 3.9+ and ffmpeg/ffprobe available on PATH.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

EPSILON = 1e-7
DURATION_TOLERANCE = 0.15
CHAPTER_TOLERANCE = 0.03

EXIT_GENERIC = 1
EXIT_ARGUMENTS = 2
EXIT_DEPENDENCY = 3
EXIT_PROCESSING = 4
EXIT_VERIFICATION = 5
EXIT_INTERRUPTED = 130

ACTIVE_PROCESSES: set[subprocess.Popen[str]] = set()
ACTIVE_PROCESSES_LOCK = threading.Lock()
PRINT_LOCK = threading.Lock()


class CompressorError(RuntimeError):
    """Base error for an expected processing failure."""


class DependencyError(CompressorError):
    """ffmpeg or ffprobe is unavailable."""


class VerificationError(CompressorError):
    """The produced output does not satisfy required checks."""


@dataclass(frozen=True)
class Silence:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class TimelineSegment:
    original_start: float
    original_end: float
    output_start: float
    output_end: float
    kind: str
    original_duration: float = field(init=False)
    output_duration: float = field(init=False)
    removed_duration: float = field(init=False)
    local_ratio: float = field(init=False)
    playback_speed: float = field(init=False)

    def __post_init__(self) -> None:
        self.original_duration = self.original_end - self.original_start
        self.output_duration = self.output_end - self.output_start
        if self.original_duration <= EPSILON:
            raise ValueError("Timeline segment must have positive original duration")
        if self.output_duration < -EPSILON:
            raise ValueError("Timeline segment cannot have negative output duration")
        self.removed_duration = self.original_duration - self.output_duration
        self.local_ratio = self.output_duration / self.original_duration
        self.playback_speed = math.inf if self.output_duration <= EPSILON else 1.0 / self.local_ratio


@dataclass(frozen=True)
class Chapter:
    index: int
    start_time: float
    end_time: float
    metadata: dict[str, str]


@dataclass(frozen=True)
class InputInfo:
    duration: float
    streams: list[dict[str, Any]]
    chapters: list[Chapter]
    format_tags: dict[str, str]
    audio_stream: dict[str, Any]
    cover_streams: list[dict[str, Any]]
    other_streams: list[dict[str, Any]]


def run_command(args: list[str], *, capture_output: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(
            args,
            shell=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
        )
    except OSError as exc:
        raise CompressorError(f"Unable to execute {args[0]!r}: {exc}") from exc
    with ACTIVE_PROCESSES_LOCK:
        ACTIVE_PROCESSES.add(process)
    try:
        stdout, stderr = process.communicate()
        return subprocess.CompletedProcess(args, process.returncode, stdout or "", stderr or "")
    finally:
        with ACTIVE_PROCESSES_LOCK:
            ACTIVE_PROCESSES.discard(process)


def terminate_active_processes() -> None:
    with ACTIVE_PROCESSES_LOCK:
        processes = list(ACTIVE_PROCESSES)
    for process in processes:
        if process.poll() is None:
            process.terminate()


def check_dependencies() -> None:
    missing = [program for program in ("ffmpeg", "ffprobe") if shutil.which(program) is None]
    if missing:
        raise DependencyError("Required executable(s) not found on PATH: " + ", ".join(missing))


def ensure_ok(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no diagnostic output").strip()
        raise CompressorError(f"{label} failed (exit code {result.returncode}):\n{detail}")


def as_float(value: Any, field_name: str) -> float:
    try:
        value_float = float(value)
    except (TypeError, ValueError) as exc:
        raise CompressorError(f"Invalid numeric {field_name}: {value!r}") from exc
    if not math.isfinite(value_float):
        raise CompressorError(f"Non-finite {field_name}: {value!r}")
    return value_float


def probe_media(path: Path) -> InputInfo:
    result = run_command([
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format",
        "-show_streams", "-show_chapters", str(path),
    ])
    ensure_ok(result, "ffprobe input inspection")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CompressorError("ffprobe returned invalid JSON") from exc

    streams = data.get("streams", [])
    if not isinstance(streams, list):
        raise CompressorError("ffprobe JSON has an invalid streams field")
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not audio_streams:
        raise CompressorError("Input contains no audio stream")

    format_data = data.get("format", {})
    duration_value = format_data.get("duration")
    if duration_value is None:
        duration_value = audio_streams[0].get("duration")
    duration = as_float(duration_value, "media duration")
    if duration <= 0:
        raise CompressorError("Input duration must be greater than zero")

    chapters: list[Chapter] = []
    for raw in data.get("chapters", []):
        start = as_float(raw.get("start_time"), "chapter start_time")
        end = as_float(raw.get("end_time"), "chapter end_time")
        if end + EPSILON < start:
            raise CompressorError(f"Chapter {raw.get('id', '?')} ends before it starts")
        tags = raw.get("tags") or {}
        chapters.append(Chapter(
            index=int(raw.get("id", len(chapters))),
            start_time=max(0.0, start),
            end_time=max(0.0, end),
            metadata={str(key): str(value) for key, value in tags.items()},
        ))

    cover_streams = [
        stream for stream in streams
        if stream.get("codec_type") == "video"
        and (stream.get("disposition") or {}).get("attached_pic", 0) == 1
    ]
    other_streams = [
        stream for stream in streams
        if stream.get("codec_type") not in {"audio"}
        and stream not in cover_streams
    ]
    tags = format_data.get("tags") or {}
    return InputInfo(
        duration=duration,
        streams=streams,
        chapters=chapters,
        format_tags={str(key): str(value) for key, value in tags.items()},
        audio_stream=audio_streams[0],
        cover_streams=cover_streams,
        other_streams=other_streams,
    )


def detect_silences(path: Path, threshold_db: float, min_duration: float, media_duration: float) -> list[Silence]:
    filter_expr = f"silencedetect=noise={threshold_db:.12g}dB:d={min_duration:.12g}"
    result = run_command([
        "ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:a:0",
        "-af", filter_expr, "-f", "null", "-",
    ])
    ensure_ok(result, "ffmpeg silence detection")

    starts: list[float] = []
    raw: list[Silence] = []
    start_re = re.compile(r"silence_start:\s*([-+]?\d+(?:\.\d+)?)")
    end_re = re.compile(r"silence_end:\s*([-+]?\d+(?:\.\d+)?)")
    for line in (result.stderr or "").splitlines():
        start_match = start_re.search(line)
        if start_match:
            starts.append(float(start_match.group(1)))
        end_match = end_re.search(line)
        if end_match:
            end = float(end_match.group(1))
            if starts:
                start = starts.pop(0)
                raw.append(Silence(start, end))
    for start in starts:
        if start < media_duration - EPSILON:
            raw.append(Silence(start, media_duration))
    return normalize_silences(raw, media_duration)


def normalize_silences(silences: Iterable[Silence], duration: float, epsilon: float = EPSILON) -> list[Silence]:
    candidates: list[Silence] = []
    for silence in silences:
        start = min(max(silence.start, 0.0), duration)
        end = min(max(silence.end, 0.0), duration)
        if end - start > epsilon:
            candidates.append(Silence(start, end))
    candidates.sort(key=lambda item: (item.start, item.end))

    merged: list[Silence] = []
    for silence in candidates:
        if not merged or silence.start > merged[-1].end + epsilon:
            merged.append(silence)
        else:
            previous = merged[-1]
            merged[-1] = Silence(previous.start, max(previous.end, silence.end))
    return merged


def calculate_output_silence_duration(duration: float, compression_ratio: float, max_output_silence: Optional[float]) -> float:
    output = duration * compression_ratio
    if max_output_silence is not None:
        output = min(output, max_output_silence)
    return output


def build_timeline(
    duration: float,
    detected_silences: Iterable[Silence],
    min_silence: float,
    compression_ratio: float,
    max_output_silence: Optional[float],
) -> list[TimelineSegment]:
    normalized = normalize_silences(detected_silences, duration)
    selected = [silence for silence in normalized if silence.duration + EPSILON >= min_silence]
    segments: list[TimelineSegment] = []
    original_cursor = 0.0
    output_cursor = 0.0

    def add_segment(start: float, end: float, kind: str, output_duration: Optional[float] = None) -> None:
        nonlocal output_cursor
        if end - start <= EPSILON:
            return
        original_duration = end - start
        effective_output = original_duration if output_duration is None else output_duration
        segment = TimelineSegment(
            original_start=start,
            original_end=end,
            output_start=output_cursor,
            output_end=output_cursor + effective_output,
            kind=kind,
        )
        segments.append(segment)
        output_cursor = segment.output_end

    for silence in selected:
        add_segment(original_cursor, silence.start, "normal")
        compressed_duration = calculate_output_silence_duration(
            silence.duration, compression_ratio, max_output_silence
        )
        add_segment(silence.start, silence.end, "silence", compressed_duration)
        original_cursor = silence.end
    add_segment(original_cursor, duration, "normal")

    if not segments:
        raise CompressorError("Could not build a valid audio timeline")
    return segments


def map_time(timestamp: float, timeline: list[TimelineSegment]) -> float:
    if timestamp <= 0:
        return 0.0
    for segment in timeline:
        if timestamp <= segment.original_end + EPSILON:
            clamped = min(max(timestamp, segment.original_start), segment.original_end)
            return segment.output_start + (clamped - segment.original_start) * segment.local_ratio
    return timeline[-1].output_end


def retime_chapters(chapters: Iterable[Chapter], timeline: list[TimelineSegment]) -> list[Chapter]:
    retimed: list[Chapter] = []
    output_duration = timeline[-1].output_end
    for chapter in chapters:
        start = map_time(chapter.start_time, timeline)
        end = map_time(chapter.end_time, timeline)
        start = min(max(0.0, start), output_duration)
        end = min(max(start, end), output_duration)
        retimed.append(Chapter(chapter.index, start, end, chapter.metadata))
    return retimed


def build_atempo_chain(speed: float) -> str:
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError(f"Invalid atempo speed: {speed}")
    factors: list[float] = []
    remaining = speed
    while remaining > 2.0 + 1e-12:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5 - 1e-12:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return ",".join(f"atempo={factor:.12g}" for factor in factors)


def ff_time(value: float) -> str:
    return f"{max(0.0, value):.9f}"


def build_audio_filter(timeline: list[TimelineSegment]) -> str:
    chains: list[str] = []
    labels: list[str] = []
    for index, segment in enumerate(timeline):
        label = f"a{index}"
        trim = (
            f"[0:a:0]atrim=start={ff_time(segment.original_start)}:"
            f"end={ff_time(segment.original_end)},asetpts=PTS-STARTPTS"
        )
        if segment.kind == "silence":
            trim += "," + build_atempo_chain(segment.playback_speed)
        chains.append(trim + f"[{label}]")
        labels.append(f"[{label}]")
    chains.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[aout]")
    return ";\n".join(chains) + ";\n"


def escape_ffmetadata(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("#", "\\#").replace(";", "\\;").replace("=", "\\=")


def build_ffmetadata(chapters: Iterable[Chapter], path: Path) -> None:
    lines = [";FFMETADATA1"]
    for chapter in chapters:
        start_ms = int(round(chapter.start_time * 1000))
        end_ms = int(round(chapter.end_time * 1000))
        if end_ms < start_ms:
            end_ms = start_ms
        lines.extend(["[CHAPTER]", "TIMEBASE=1/1000", f"START={start_ms}", f"END={end_ms}"])
        for key, value in chapter.metadata.items():
            lines.append(f"{escape_ffmetadata(key)}={escape_ffmetadata(value)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def audio_codec_args(info: InputInfo, audio_codec: Optional[str], audio_bitrate: Optional[str]) -> list[str]:
    codec = audio_codec or "aac"
    args = ["-c:a", codec]
    if audio_bitrate:
        args.extend(["-b:a", audio_bitrate])
    elif codec == "aac":
        bit_rate = info.audio_stream.get("bit_rate")
        try:
            source_rate = int(bit_rate)
        except (TypeError, ValueError):
            source_rate = 64000
        args.extend(["-b:a", str(max(32000, min(source_rate, 512000)))])
    sample_rate = info.audio_stream.get("sample_rate")
    if sample_rate:
        args.extend(["-ar", str(sample_rate)])
    channels = info.audio_stream.get("channels")
    if channels:
        args.extend(["-ac", str(channels)])
    return args


@dataclass(frozen=True)
class ChapterResult:
    chapter: Chapter
    path: Path
    duration: float
    removed_duration: float
    silence_count: int
    resumed: bool


def stable_signature(data: dict[str, Any]) -> str:
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def chapter_units(info: InputInfo) -> list[Chapter]:
    if not info.chapters:
        raise CompressorError("Input has no embedded chapters; chapter-based processing cannot continue")
    ordered = sorted(info.chapters, key=lambda chapter: (chapter.start_time, chapter.index))
    for previous, current in zip(ordered, ordered[1:]):
        if current.start_time <= previous.start_time + EPSILON:
            raise CompressorError("Chapter start times must be strictly increasing")

    units: list[Chapter] = []
    for position, chapter in enumerate(ordered):
        start = 0.0 if position == 0 else chapter.start_time
        end = ordered[position + 1].start_time if position + 1 < len(ordered) else info.duration
        if end <= start + EPSILON:
            raise CompressorError(f"Chapter {position + 1} has no processable duration")
        units.append(Chapter(chapter.index, start, end, chapter.metadata))
    return units


def extract_chapter(input_path: Path, source_path: Path, chapter: Chapter) -> None:
    temporary = source_path.with_suffix(".flac.tmp")
    temporary.unlink(missing_ok=True)
    result = run_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(input_path),
        "-ss", ff_time(chapter.start_time), "-t", ff_time(chapter.end_time - chapter.start_time),
        "-map", "0:a:0", "-vn", "-map_metadata", "-1", "-c:a", "flac", "-f", "flac", str(temporary),
    ])
    ensure_ok(result, f"extracting chapter {chapter.index}")
    os.replace(temporary, source_path)


def render_chapter(
    source_path: Path,
    output_path: Path,
    filter_path: Path,
    info: InputInfo,
    timeline: list[TimelineSegment],
    audio_codec: Optional[str],
    audio_bitrate: Optional[str],
) -> None:
    filter_path.write_text(build_audio_filter(timeline), encoding="utf-8")
    temporary = output_path.with_suffix(".m4a.tmp")
    temporary.unlink(missing_ok=True)
    result = run_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source_path),
        "-/filter_complex", str(filter_path), "-map", "[aout]", "-map_metadata", "-1",
        *audio_codec_args(info, audio_codec, audio_bitrate),
        "-movflags", "+faststart", "-f", "mp4", str(temporary),
    ])
    ensure_ok(result, f"processing {source_path.parent.name}")
    os.replace(temporary, output_path)


def process_chapter(
    input_path: Path,
    input_identity: dict[str, Any],
    info: InputInfo,
    chapter: Chapter,
    position: int,
    total: int,
    work_dir: Path,
    args: argparse.Namespace,
) -> ChapterResult:
    chapter_dir = work_dir / f"{position:04d}"
    chapter_dir.mkdir(parents=True, exist_ok=True)
    state_path = chapter_dir / "state.json"
    source_path = chapter_dir / "source.flac"
    output_path = chapter_dir / "processed.m4a"
    filter_path = chapter_dir / "audio_filter.txt"
    source_signature = stable_signature({
        "version": 1,
        "input": input_identity,
        "start": chapter.start_time,
        "end": chapter.end_time,
    })
    processing_signature = stable_signature({
        "source": source_signature,
        "silence_threshold": args.silence_threshold,
        "min_silence": args.min_silence,
        "compression_ratio": args.compression_ratio,
        "max_output_silence": args.max_output_silence,
        "audio_codec": args.audio_codec,
        "audio_bitrate": args.audio_bitrate,
    })
    state = load_state(state_path)
    if (
        state.get("status") == "complete"
        and state.get("processing_signature") == processing_signature
        and output_path.is_file()
        and output_path.stat().st_size > 0
    ):
        with PRINT_LOCK:
            print(f"[{position}/{total}] already complete")
        return ChapterResult(
            chapter, output_path, as_float(state.get("output_duration"), "saved output duration"),
            as_float(state.get("removed_duration", 0), "saved removed duration"),
            int(state.get("silence_count", 0)), True,
        )

    try:
        if state.get("source_signature") != source_signature or not source_path.is_file():
            with PRINT_LOCK:
                print(f"[{position}/{total}] extracting")
            extract_chapter(input_path, source_path, chapter)
            state = {
                "version": 1,
                "status": "extracted",
                "source_signature": source_signature,
                "chapter": position,
                "title": chapter_title(chapter),
            }
            save_state(state_path, state)

        with PRINT_LOCK:
            print(f"[{position}/{total}] detecting silences and processing")
        source_info = probe_media(source_path)
        detected = detect_silences(
            source_path, args.silence_threshold, args.min_silence, source_info.duration
        )
        timeline = build_timeline(
            source_info.duration, detected, args.min_silence,
            args.compression_ratio, args.max_output_silence,
        )
        silence_segments = [segment for segment in timeline if segment.kind == "silence"]
        render_chapter(
            source_path, output_path, filter_path, info, timeline, args.audio_codec, args.audio_bitrate
        )
        output_duration = probe_media(output_path).duration
        removed_duration = sum(segment.removed_duration for segment in silence_segments)
        state.update({
            "status": "complete",
            "processing_signature": processing_signature,
            "output_duration": output_duration,
            "removed_duration": removed_duration,
            "silence_count": len(silence_segments),
        })
        save_state(state_path, state)
        with PRINT_LOCK:
            print(f"[{position}/{total}] complete")
        return ChapterResult(
            chapter, output_path, output_duration, removed_duration, len(silence_segments), False
        )
    except Exception as exc:
        state.update({"status": "failed", "error": str(exc)})
        save_state(state_path, state)
        raise


def retimed_chapters_from_results(results: list[ChapterResult]) -> list[Chapter]:
    chapters: list[Chapter] = []
    cursor = 0.0
    for result in results:
        end = cursor + result.duration
        chapters.append(Chapter(result.chapter.index, cursor, end, result.chapter.metadata))
        cursor = end
    return chapters


def ffconcat_escape(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "'\\''")


def rebuild_audiobook(
    input_path: Path,
    output_path: Path,
    info: InputInfo,
    results: list[ChapterResult],
    chapters: list[Chapter],
    work_dir: Path,
) -> None:
    concat_path = work_dir / "chapters.ffconcat"
    metadata_path = work_dir / "chapters.ffmeta"
    temporary_output = work_dir / "reconstructed.m4b.tmp"
    concat_lines = ["ffconcat version 1.0"]
    for result in results:
        concat_lines.extend([f"file '{ffconcat_escape(result.path)}'", f"duration {result.duration:.9f}"])
    concat_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
    build_ffmetadata(chapters, metadata_path)
    temporary_output.unlink(missing_ok=True)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_path),
        "-i", str(input_path), "-i", str(metadata_path),
        "-map", "0:a:0",
    ]
    if info.cover_streams:
        command.extend(["-map", "1:v?"])
    command.extend(["-map_metadata", "1", "-map_chapters", "2", "-c:a", "copy"])
    if info.cover_streams:
        command.extend(["-c:v", "copy", "-disposition:v", "attached_pic"])
    command.extend(["-movflags", "+faststart", "-f", "mp4", str(temporary_output)])
    result = run_command(command)
    ensure_ok(result, "reconstructing audiobook")
    if output_path.exists():
        output_path.unlink()
    os.replace(temporary_output, output_path)


def chapter_title(chapter: Chapter) -> str:
    return chapter.metadata.get("title", "")


def verify_output(
    output_path: Path,
    input_info: InputInfo,
    expected_duration: float,
    expected_chapters: list[Chapter],
) -> None:
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise VerificationError("Output file was not created or is empty")
    output_info = probe_media(output_path)
    if abs(output_info.duration - expected_duration) > DURATION_TOLERANCE:
        raise VerificationError(
            f"Output duration mismatch: expected about {expected_duration:.3f}s, got {output_info.duration:.3f}s "
            f"(tolerance {DURATION_TOLERANCE:.3f}s)"
        )
    if not output_info.audio_stream:
        raise VerificationError("Output has no audio stream")
    if len(output_info.chapters) != len(expected_chapters):
        raise VerificationError(
            f"Chapter count mismatch: expected {len(expected_chapters)}, got {len(output_info.chapters)}"
        )
    for expected, actual in zip(expected_chapters, output_info.chapters):
        if chapter_title(expected) != chapter_title(actual):
            raise VerificationError(
                f"Chapter title mismatch: expected {chapter_title(expected)!r}, got {chapter_title(actual)!r}"
            )
        if abs(expected.start_time - actual.start_time) > CHAPTER_TOLERANCE:
            raise VerificationError(
                f"Chapter start mismatch for {chapter_title(expected)!r}: expected {expected.start_time:.3f}, "
                f"got {actual.start_time:.3f}"
            )
        if abs(expected.end_time - actual.end_time) > CHAPTER_TOLERANCE:
            raise VerificationError(
                f"Chapter end mismatch for {chapter_title(expected)!r}: expected {expected.end_time:.3f}, "
                f"got {actual.end_time:.3f}"
            )
    if input_info.cover_streams and not output_info.cover_streams:
        raise VerificationError("Input has attached cover art but output does not")


def format_duration(value: float) -> str:
    total_ms = int(round(value * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def validate_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    input_path = Path(args.input).expanduser()
    output_path = Path(args.output).expanduser()
    if not input_path.is_file():
        raise ValueError(f"Input does not exist or is not a regular file: {input_path}")
    if not os.access(input_path, os.R_OK):
        raise ValueError(f"Input is not readable: {input_path}")
    try:
        same = input_path.resolve() == output_path.resolve()
    except OSError:
        same = input_path.absolute() == output_path.absolute()
    if same:
        raise ValueError("Input and output must be different files")
    if output_path.exists() and not args.overwrite:
        raise ValueError(f"Output already exists: {output_path}. Use --overwrite to replace it.")
    if output_path.parent and not output_path.parent.exists():
        raise ValueError(f"Output directory does not exist: {output_path.parent}")
    return input_path, output_path


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compress silences in an M4B while preserving metadata, cover art, and retimed chapters."
    )
    parser.add_argument("input", help="Input .m4b file")
    parser.add_argument("output", help="Output .m4b file")
    parser.add_argument("--silence-threshold", type=float, default=-40.0, metavar="DB",
                        help="Silence threshold in dB (default: -40)")
    parser.add_argument("--min-silence", type=float, default=0.5, metavar="SECONDS",
                        help="Minimum silence duration to modify (default: 0.5)")
    parser.add_argument("--compression-ratio", type=float, default=0.5, metavar="RATIO",
                        help="Output/original duration ratio for selected silences (default: 0.5)")
    parser.add_argument("--max-output-silence", type=float, default=None, metavar="SECONDS",
                        help="Optional cap on residual duration of each selected silence")
    parser.add_argument("--audio-codec", default=None, help="Audio encoder, default aac")
    parser.add_argument("--audio-bitrate", default=None, help="Audio bitrate, e.g. 96k")
    parser.add_argument("--workers", type=int, default=max(1, min(4, os.cpu_count() or 1)), metavar="N",
                        help="Chapters processed concurrently (default: up to 4)")
    parser.add_argument("--work-dir", default=None, metavar="PATH",
                        help="Chapter directory (default: OUTPUT.chapters; deleted after success unless retained)")
    parser.add_argument("--keep-work-dir", action="store_true",
                        help="Keep the chapter directory after successful processing")
    parser.add_argument("--overwrite", action="store_true", help="Atomically replace an existing output file")
    parser.add_argument("--verbose", action="store_true", help="Print each selected silence")
    args = parser.parse_args(argv)
    if args.min_silence <= 0:
        parser.error("--min-silence must be greater than zero")
    if not 0 < args.compression_ratio <= 1:
        parser.error("--compression-ratio must be greater than zero and at most one")
    if args.max_output_silence is not None and args.max_output_silence <= 0:
        parser.error("--max-output-silence must be greater than zero")
    if not math.isfinite(args.silence_threshold):
        parser.error("--silence-threshold must be finite")
    if args.workers <= 0:
        parser.error("--workers must be greater than zero")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        input_path, output_path = validate_paths(args)
        check_dependencies()
        info = probe_media(input_path)
        units = chapter_units(info)
        work_dir = (
            Path(args.work_dir).expanduser()
            if args.work_dir
            else output_path.parent / f"{output_path.stem}.chapters"
        )
        resolved_work_dir = work_dir.resolve()
        if input_path.resolve().is_relative_to(resolved_work_dir) or output_path.resolve().is_relative_to(resolved_work_dir):
            raise ValueError("--work-dir must not contain the input or output file")
        work_dir.mkdir(parents=True, exist_ok=True)
        input_stat = input_path.stat()
        input_identity = {
            "path": str(input_path.resolve()),
            "size": input_stat.st_size,
            "mtime_ns": input_stat.st_mtime_ns,
        }
        print(f"Input: {input_path}")
        print(f"Output: {output_path}")
        print(f"Chapter workspace: {work_dir}")
        print()
        print(f"Input duration: {format_duration(info.duration)} ({info.duration:.3f} s)")
        print(f"Chapters: {len(units)}")
        print(f"Concurrent workers: {args.workers}")
        print(f"Silence threshold: {args.silence_threshold:g} dB")
        print(f"Minimum silence: {args.min_silence:.6g} s")
        print(f"Compression ratio: {args.compression_ratio:.6g}")
        print("Maximum output silence: " + (
            f"{args.max_output_silence:.6g} s" if args.max_output_silence is not None else "disabled"
        ))
        if info.other_streams:
            print(f"Warning: {len(info.other_streams)} non-audio/non-cover stream(s) will not be preserved.", file=sys.stderr)
        print()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
        futures = [
            executor.submit(
                process_chapter, input_path, input_identity, info, chapter, position,
                len(units), work_dir, args,
            )
            for position, chapter in enumerate(units, start=1)
        ]
        try:
            results = [future.result() for future in futures]
        except KeyboardInterrupt:
            for future in futures:
                future.cancel()
            terminate_active_processes()
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)

        expected_chapters = retimed_chapters_from_results(results)
        expected_duration = sum(result.duration for result in results)
        removed = sum(result.removed_duration for result in results)
        selected_count = sum(result.silence_count for result in results)
        resumed_count = sum(result.resumed for result in results)
        print()
        print(f"Silences selected: {selected_count}")
        print(f"Removed duration: {removed:.6f} s")
        print(f"Expected output duration: {format_duration(expected_duration)} ({expected_duration:.6f} s)")
        print(f"Chapters reused from saved state: {resumed_count}")
        if args.verbose:
            for position, result in enumerate(results, start=1):
                print(
                    f"Chapter {position:04d}: {result.silence_count} silence(s), "
                    f"{result.removed_duration:.6f} s removed, {result.duration:.6f} s output"
                )

        candidate_output = work_dir / "reconstructed.m4b"
        print("Reconstructing audiobook...")
        rebuild_audiobook(input_path, candidate_output, info, results, expected_chapters, work_dir)
        print("Verifying...")
        verify_output(candidate_output, info, expected_duration, expected_chapters)
        if output_path.exists():
            output_path.unlink()
        os.replace(candidate_output, output_path)
        if not args.keep_work_dir:
            print("Removing chapter workspace...")
            shutil.rmtree(work_dir)
        print("Done.")
        return 0
    except KeyboardInterrupt:
        terminate_active_processes()
        print("Interrupted. Completed chapter states were preserved; run the same command to resume.", file=sys.stderr)
        return EXIT_INTERRUPTED
    except DependencyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_DEPENDENCY
    except VerificationError as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        return EXIT_VERIFICATION
    except ValueError as exc:
        print(f"Argument/path error: {exc}", file=sys.stderr)
        return EXIT_ARGUMENTS
    except CompressorError as exc:
        print(f"Processing error: {exc}", file=sys.stderr)
        return EXIT_PROCESSING


if __name__ == "__main__":
    sys.exit(main())
