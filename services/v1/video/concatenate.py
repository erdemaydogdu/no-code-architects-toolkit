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
import requests
from services.file_management import download_file
from config import LOCAL_STORAGE_PATH


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

        
def process_video_concatenate(
    media_urls,
    job_id,
    webhook_url=None,
    *,
    use_transitions=False,
    transitions="fade",                 # str or list[str] for each join
    transition_durations=1.0,           # float or list[float] for each join
    width=1280, height=720, fps=30,
    preserve_clip_starts=True,          # <-- NEW: add lead-in equal to transition duration
    pad_color="black"                   # color for the lead-in frames
):
    """
    Combine multiple videos. In transition mode:
    - Add a lead-in (video black + audio silence) to each clip except the first,
      equal to the corresponding transition duration, so speech at the start is preserved.
    """
    input_files = []
    output_filename = f"{job_id}.mp4"
    output_path = os.path.join(LOCAL_STORAGE_PATH, output_filename)

    try:
        # 1) Download inputs
        for i, media_item in enumerate(media_urls):
            url = media_item['video_url']
            input_filename = download_file(url, os.path.join(LOCAL_STORAGE_PATH, f"{job_id}_input_{i}"))
            input_files.append(input_filename)

        # Fast path
        if len(input_files) == 1 and not use_transitions:
            (
                ffmpeg.input(input_files[0])
                .output(output_path, c='copy')
                .overwrite_output()
                .run(capture_stdout=True, capture_stderr=True)
            )

        elif not use_transitions:
            # Concat demuxer (no re-encode)
            concat_file_path = os.path.join(LOCAL_STORAGE_PATH, f"{job_id}_concat_list.txt")
            with open(concat_file_path, 'w') as f:
                for p in input_files:
                    f.write(f"file '{os.path.abspath(p)}'\n")

            (
                ffmpeg.input(concat_file_path, format='concat', safe=0)
                .output(output_path, c='copy')
                .overwrite_output()
                .run(capture_stdout=True, capture_stderr=True)
            )
            os.remove(concat_file_path)

        else:
            # Transition mode (re-encode)
            if len(input_files) < 2:
                raise ValueError("At least two inputs are required for transitions.")

            durations = [_probe_duration(p) for p in input_files]
            audio_flags = [_has_audio_stream(p) for p in input_files]

            joins = len(input_files) - 1
            transitions_norm = _normalize_list(transitions, joins)
            d_norm = [float(x) for x in _normalize_list(transition_durations, joins)]

            # Lead-in per clip (seconds): prepad[0]=0; prepad[i]=d_norm[i-1] if preserving starts
            prepad = ([0.0] + d_norm[:]) if preserve_clip_starts else [0.0] * len(input_files)

            filter_lines = []
            v_labels, a_labels = [], []

            for idx, _ in enumerate(input_files):
                vlab = f"v{idx}"
                alab = f"a{idx}"

                # --- Reset PTS first, then normalize, then optional lead-in on video ---
                if preserve_clip_starts and prepad[idx] > 0:
                    filter_lines.append(
                        f"[{idx}:v]setpts=PTS-STARTPTS,"
                        f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease,"
                        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
                        f"tpad=start_duration={prepad[idx]}:color={pad_color}[{vlab}]"
                    )
                else:
                    filter_lines.append(
                        f"[{idx}:v]setpts=PTS-STARTPTS,"
                        f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease,"
                        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[{vlab}]"
                    )

                # --- Audio: reset PTS, stereo+48k, resync, optional lead-in via adelay ---
                if audio_flags[idx]:
                    if preserve_clip_starts and prepad[idx] > 0:
                        ms = int(round(prepad[idx] * 1000))
                        filter_lines.append(
                            f"[{idx}:a]asetpts=PTS-STARTPTS,"
                            f"aformat=channel_layouts=stereo,aresample=48000:async=1:first_pts=0,"
                            f"adelay={ms}|{ms}[{alab}]"
                        )
                    else:
                        filter_lines.append(
                            f"[{idx}:a]asetpts=PTS-STARTPTS,"
                            f"aformat=channel_layouts=stereo,aresample=48000:async=1:first_pts=0[{alab}]"
                        )
                else:
                    # Synthesize silence for (duration + lead-in) if clip has no audio
                    total_sil = durations[idx] + (prepad[idx] if preserve_clip_starts else 0.0)
                    filter_lines.append(
                        f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=0:{total_sil:.6f},"
                        f"asetpts=PTS-STARTPTS[{alab}]"
                    )

                v_labels.append(vlab)
                a_labels.append(alab)

            # --- Chain xfade/acrossfade with iterative offsets to avoid freezing ---
            current_v, current_a = v_labels[0], a_labels[0]
            current_len = durations[0]  # current composed timeline length after first clip (no prepad on clip 0)

            for k in range(1, len(input_files)):
                trans = str(transitions_norm[k-1])
                d_k = float(d_norm[k-1])
                prepad_k = prepad[k]

                # Start xfade so that it ends exactly at the boundary of the previous composed timeline
                offset = max(current_len - d_k, 0.0)

                next_v, next_a = v_labels[k], a_labels[k]
                out_v, out_a = f"v{k}o", f"a{k}o"

                filter_lines.append(
                    f"[{current_v}][{next_v}]xfade=transition={trans}:duration={d_k}:offset={offset:.6f}[{out_v}]"
                )
                filter_lines.append(
                    f"[{current_a}][{next_a}]acrossfade=d={d_k}[{out_a}]"
                )

                # Update composed length: add lead-in and clip k, subtract overlap d_k
                current_len = current_len + prepad_k + durations[k] - d_k

                current_v, current_a = out_v, out_a

            filter_complex = "; ".join(filter_lines)

            # Execute via subprocess to map labeled pads safely, enable genpts for container safety
            cmd = ["ffmpeg", "-y"]
            for p in input_files:
                cmd += ["-i", p]
            cmd += [
                "-filter_complex", filter_complex,
                "-map", f"[{current_v}]",
                "-map", f"[{current_a}]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "aac",
                "-movflags", "+faststart",
                "-fflags", "+genpts",
                output_path
            ]
            subprocess.run(cmd, check=True)

        # Cleanup
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