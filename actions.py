import os, subprocess, re, shutil
from grav1ty.util import ffmpeg

merge_out = "merged"

def merge(logger, projects, project):
  os.makedirs(merge_out, exist_ok=True)
  out_name = f"{os.path.splitext(os.path.basename(project.path_in))[0]}.mkv"

  cmd = [
    "ffmpeg", "-y",
    "-i", project.path_out,
    "-i", project.path_in,
    "-map_metadata", "-1",
    "-map", "0:v:0",
    "-map", "1:a:0",
    "-c:v", "copy",
    "-c:a", "copy",
    os.path.join(merge_out, out_name)
  ]

  logger.add("auto", project.projectid, "merging")
  ffmpeg(cmd, lambda x: logger.add("auto", "merging", project.projectid, f"{x}/{project.total_frames}", cr=True))

actions = {"merge": merge}
