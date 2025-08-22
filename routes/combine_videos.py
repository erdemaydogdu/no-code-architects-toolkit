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



from flask import Blueprint
from app_utils import *
import logging
from services.ffmpeg_toolkit import process_video_combination
from services.authentication import authenticate
from services.cloud_storage import upload_file

combine_bp = Blueprint('combine', __name__)
logger = logging.getLogger(__name__)

@combine_bp.route('/combine-videos', methods=['POST'])
@authenticate
@validate_payload({
    "type": "object",
    "properties": {
        "video_urls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "video_url": {"type": "string", "format": "uri"}
                },
                "required": ["video_url"]
            },
            "minItems": 1
        },
        "webhook_url": {"type": "string", "format": "uri"},
        "id": {"type": "string"},

        # --- Optional transition settings (backward compatible) ---
        "use_transitions": {"type": "boolean"},
        # Accept either a single string or an array of strings
        "transitions": {
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}, "minItems": 1}
            ]
        },
        # Accept either a single number or an array of numbers
        "transition_durations": {
            "oneOf": [
                {"type": "number"},
                {"type": "array", "items": {"type": "number"}, "minItems": 1}
            ]
        },
        # Normalization parameters used only when transitions are enabled
        "width":  {"type": "integer", "minimum": 2},
        "height": {"type": "integer", "minimum": 2},
        "fps":    {"type": "integer", "minimum": 1}
    },
    "required": ["video_urls"],
    "additionalProperties": False
})
@queue_task_wrapper(bypass_queue=False)
def combine_videos(job_id, data):
    media_urls = data['video_urls']
    webhook_url = data.get('webhook_url')
    id = data.get('id')

    # Extract optional transition parameters with safe defaults
    use_transitions = bool(data.get('use_transitions', False))
    transitions = data.get('transitions', "fade")
    transition_durations = data.get('transition_durations', 1.0)
    width = int(data.get('width', 1280))
    height = int(data.get('height', 720))
    fps = int(data.get('fps', 30))

    logger.info(
        f"Job {job_id}: Received combine-videos request | "
        f"clips={len(media_urls)} | transitions={use_transitions} | "
        f"trans={transitions} | d={transition_durations} | "
        f"norm={width}x{height}@{fps} | id={id}"
    )

    try:
           # Delegate to ffmpeg toolkit with optional transition settings
        output_file = process_video_combination(
            media_urls,
            job_id,
            webhook_url=webhook_url,
            use_transitions=use_transitions,
            transitions=transitions,
            transition_durations=transition_durations,
            width=width,
            height=height,
            fps=fps
        )
        logger.info(f"Job {job_id}: Video combination process completed successfully")

        cloud_url = upload_file(output_file)
        logger.info(f"Job {job_id}: Combined video uploaded to cloud storage: {cloud_url}")

        return cloud_url, "/combine-videos", 200

    except Exception as e:
        logger.error(f"Job {job_id}: Error during video combination process - {str(e)}")
        return str(e), "/combine-videos", 500