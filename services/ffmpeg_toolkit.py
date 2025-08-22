# Copyright (c) 2025 Stephen G. Pope
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.



import os
import json
import subprocess
import ffmpeg
from services.file_management import download_file
from config import STORAGE_PATH

# --- Helpers ---------------------------------------------------------------

def _probe_duration(path):
    """Get media duration in seconds using ffprobe (format duration)."""
    r = subprocess.run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", path
    ], capture_output=True, text=True, check=True)
    data = json.loads(r.stdout)
    return float(data["format"]["duration"])

def _has_audio_stream(path):
    """Return True if the media file contains at least one audio stream."""
    r = subprocess.run([
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "csv=p=0", path
    ], capture_output=True, text=True)
    return bool(r.stdout.strip())

def _normalize_list(value, target_len):
    """
    Normalize a parameter that can be either a scalar or a list to a list of length target_len.
    - If scalar: replicate it.
    - If list shorter: pad with its last value.
    - If list longer: truncate.
    """
    if isinstance(value, (str, float, int)):
        return [value] * target_len
    if not isinstance(value, (list, tuple)):
        raise ValueError("Parameter must be a scalar or a list/tuple.")
    lst = list(value)
    if len(lst) < target_len:
        lst += [lst[-1]] * (target_len - len(lst))
    return lst[:target_len]

# Set the default local storage directory
STORAGE_PATH = "/tmp/"

def process_conversion(media_url, job_id, bitrate='128k', webhook_url=None):
    """Convert media to MP3 format with specified bitrate."""
    input_filename = download_file(media_url, os.path.join(STORAGE_PATH, f"{job_id}_input"))
    output_filename = f"{job_id}.mp3"
    output_path = os.path.join(STORAGE_PATH, output_filename)

    try:
        # Convert media file to MP3 with specified bitrate
        (
            ffmpeg
            .input(input_filename)
            .output(output_path, acodec='libmp3lame', audio_bitrate=bitrate)
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True)
        )
        os.remove(input_filename)
        print(f"Conversion successful: {output_path} with bitrate {bitrate}")

        # Ensure the output file exists locally before attempting upload
        if not os.path.exists(output_path):
            raise FileNotFoundError(f"Output file {output_path} does not exist after conversion.")

        return output_path

    except Exception as e:
        print(f"Conversion failed: {str(e)}")
        raise

def process_video_combination(
    media_urls,
    job_id,
    webhook_url=None,
    *,
    use_transitions=False,
    transitions="fade",                 # str or list[str] of length (N-1)
    transition_durations=1.0,           # float or list[float] of length (N-1)
    width=1280, height=720, fps=30      # normalization when transitions are enabled
):
    """
    Combine multiple videos into one.
    - Default (use_transitions=False): concat demuxer with -c copy (fast, no re-encode).
    - Transition mode: xfade (video) + acrossfade (audio), re-encoding with libx264/aac.

    Parameters:
    - transitions: single transition name (e.g., "fade") or list per join
                   (e.g., ["fade", "wipeleft", "circleopen"]).
    - transition_durations: single float (e.g., 1.0) or list per join
                            (e.g., [0.75, 1.0, 1.25]).
    """
    input_files = []
    output_filename = f"{job_id}.mp4"
    output_path = os.path.join(STORAGE_PATH, output_filename)

    try:
        # 1) Download all media files
        for i, media_item in enumerate(media_urls):
            url = media_item['video_url']
            input_filename = download_file(url, os.path.join(STORAGE_PATH, f"{job_id}_input_{i}"))
            input_files.append(input_filename)

        # Fast path: single file + no transitions
        if len(input_files) == 1 and not use_transitions:
            (
                ffmpeg
                .input(input_files[0])
                .output(output_path, c='copy')
                .overwrite_output()
                .run(capture_stdout=True, capture_stderr=True)
            )
        elif not use_transitions:
            # 2) Fast concat using concat demuxer
            concat_file_path = os.path.join(STORAGE_PATH, f"{job_id}_concat_list.txt")
            with open(concat_file_path, 'w') as concat_file:
                for input_file in input_files:
                    concat_file.write(f"file '{os.path.abspath(input_file)}'\n")

            (
                ffmpeg
                .input(concat_file_path, format='concat', safe=0)
                .output(output_path, c='copy')
                .overwrite_output()
                .run(capture_stdout=True, capture_stderr=True)
            )
            os.remove(concat_file_path)

        else:
            # 3) Transition mode: build filter_complex with xfade + acrossfade
            if len(input_files) < 2:
                raise ValueError("At least two inputs are required for transitions.")

            # Read durations and audio presence for each input
            durations = [_probe_duration(p) for p in input_files]
            audio_flags = [_has_audio_stream(p) for p in input_files]

            # Normalize transitions and durations to (N-1) items
            joins = len(input_files) - 1
            transitions_norm = _normalize_list(transitions, joins)
            durations_norm = [float(x) for x in _normalize_list(transition_durations, joins)]

            filter_lines = []
            v_labels, a_labels = [], []

            # Normalize each input (scale, fps, audio)
            for idx, _ in enumerate(input_files):
                vlab = f"v{idx}"
                alab = f"a{idx}"
                filter_lines.append(
                    f"[{idx}:v]fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[{vlab}]"
                )
                if audio_flags[idx]:
                    filter_lines.append(f"[{idx}:a]aformat=channel_layouts=stereo,aresample=48000[{alab}]")
                else:
                    # If no audio on this clip, synthesize silence for its full duration
                    filter_lines.append(
                        f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=0:{durations[idx]:.6f},asetpts=N/SR/TB[{alab}]"
                    )
                v_labels.append(vlab)
                a_labels.append(alab)

            # Chain xfade/acrossfade with per-join transition and duration
            current_v, current_a = v_labels[0], a_labels[0]
            cum = 0.0
            for k in range(1, len(input_files)):
                prev_dur = durations[k-1]
                cum += prev_dur

                trans = str(transitions_norm[k-1])
                d = float(durations_norm[k-1])

                # offset_k = sum(dur[0..k-1]) - sum(d_i for i in 0..k-1)
                # Since we allow per-join durations, subtract the sum of previous d's
                prev_d_sum = sum(durations_norm[:k-1]) if k > 1 else 0.0
                offset = cum - prev_d_sum - d

                next_v, next_a = v_labels[k], a_labels[k]
                out_v, out_a = f"v{k}o", f"a{k}o"

                filter_lines.append(
                    f"[{current_v}][{next_v}]xfade=transition={trans}:duration={d}:offset={offset:.6f}[{out_v}]"
                )
                filter_lines.append(
                    f"[{current_a}][{next_a}]acrossfade=d={d}[{out_a}]"
                )

                current_v, current_a = out_v, out_a

            filter_complex = "; ".join(filter_lines)

            # Execute via subprocess to map labeled pads safely
            cmd = ["ffmpeg", "-y"]
            for p in input_files:
                cmd += ["-i", p]
            cmd += [
                "-filter_complex", filter_complex,
                "-map", f"[{current_v}]",
                "-map", f"[{current_a}]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "aac", "-movflags", "+faststart",
                output_path
            ]
            subprocess.run(cmd, check=True)

        # 4) Cleanup input files
        for f in input_files:
            try:
                os.remove(f)
            except:
                pass

        print(f"Video combination successful: {output_path}")

        if not os.path.exists(output_path):
            raise FileNotFoundError(f"Output file {output_path} does not exist after combination.")

        return output_path

    except Exception as e:
        print(f"Video combination failed: {e}")
        raise