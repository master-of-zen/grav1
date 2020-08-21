# -*- coding: utf-8 -*-
import os, subprocess, re, shutil, logging
from threading import Thread, Event
from grav1ty.util import ffmpeg

merge_out = "merged"

AUTO = 23
logging.addLevelName(AUTO, "AUTO")

def merge(projects, project):
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

  logging.log(AUTO, project.projectid, "merging")
  ffmpeg(cmd, lambda x: logging.log(AUTO, "merging", project.projectid, f"{x}/{project.total_frames}", extra={"cr": True}))

actions = {"merge": merge}
